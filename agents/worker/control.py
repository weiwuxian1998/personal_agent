"""Worker 控制通道（设计文档 §5.5 / §5.6.1）。

worker 进程内只做两件事：

1. **收指令**：消费 `asst:stream:worker:<task_id>`（router 的 `send_message_to_session`
   写入），由 `WorkerInboxMiddleware` 在下一轮模型调用前注入；
2. **收停止**：轮询 `asst:task:stop:<task_id>`，值 `1` = 已请求停止（允许当前工具跑完），
   `2` = 已注入收尾指令（worker 自己回写），`3` = router 已做主进程树强杀。

线程只做 IO，不改图状态；所有状态通过线程安全的方法暴露给中间件。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import redis

from bus.envelope import stop_key, worker_consumer, worker_group, worker_stream
from bus.stream import Bus

logger = logging.getLogger("worker.control")


class WorkerControl:
    def __init__(
        self,
        *,
        redis_client: redis.Redis,
        task_id: str,
        poll_interval_s: float = 0.3,
        inbox_max: int = 20,
    ) -> None:
        self._redis = redis_client
        self._task_id = task_id
        self._poll = max(0.05, float(poll_interval_s))
        self._inbox_max = max(1, int(inbox_max))

        self._lock = threading.Lock()
        self._inbox: list[str] = []
        self._stop_level = 0
        self._injected = False

        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="worker-control", daemon=True)
        self._bus: Bus | None = None

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._closed.set()
        if self._thread.is_alive():
            self._thread.join(timeout=1.5)

    # ------------------------------------------------------------------ 内部
    def _loop(self) -> None:
        bus = Bus(self._redis)
        bus.stream = worker_stream(self._task_id)
        bus.group = worker_group(self._task_id)
        bus.consumer = worker_consumer(self._task_id)
        try:
            bus.ensure_worker_group(self._task_id)
        except Exception:
            logger.warning("worker 指令流建组失败（指令通道不可用）", exc_info=True)
        self._bus = bus

        while not self._closed.is_set():
            try:
                level = str(self._redis.get(stop_key(self._task_id)) or "")
                if level.isdigit() and int(level) > self._stop_level:
                    self._stop_level = int(level)
                    logger.info("收到停止请求 task=%s level=%s", self._task_id, level)

                entries = bus.read_batch(block_ms=int(self._poll * 1000), count=10)
                if entries:
                    with self._lock:
                        for _, event in entries:
                            if event.type != "router_message":
                                continue
                            text = str(event.payload.get("text") or "").strip()
                            if not text:
                                continue
                            self._inbox.append(text)
                            if len(self._inbox) > self._inbox_max:
                                self._inbox = self._inbox[-self._inbox_max :]
                    bus.ack(entries)
            except Exception:
                logger.warning("worker 控制通道轮询失败", exc_info=True)
                self._closed.wait(1.0)

    # ------------------------------------------------------------------ 对外状态
    @property
    def stop_level(self) -> int:
        return self._stop_level

    def stop_requested(self) -> bool:
        return self._stop_level >= 1

    def injected(self) -> bool:
        return self._injected

    def drain_instructions(self) -> list[str]:
        with self._lock:
            items = self._inbox
            self._inbox = []
        return items

    def mark_injected(self) -> None:
        """收尾指令已注入：把停止级别回写为 2，router 的 `StopCommand` 能读到。"""
        self._injected = True
        try:
            self._redis.set(stop_key(self._task_id), "2", ex=3600)
        except Exception:
            logger.warning("回写停止级别失败 task=%s", self._task_id, exc_info=True)
        logger.info("已注入收尾指令 task=%s", self._task_id)
