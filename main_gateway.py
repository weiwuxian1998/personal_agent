"""Gateway 进程入口：常驻收消息 + 按需拉起 router（契约 §9.1）。

职责：
1. 渠道（飞书长连接 / 控制台）收到消息 → 进程内毫秒级返回 → 入 Redis Stream；
2. 去重：同一条 `message_id` 只入队一次（跨进程 `SET NX`，与 channel 的进程内去重互补）；
3. supervisor：在没有 router 心跳、且消费者组还有未投递/未确认事件时拉起 `main_router.py`，
   router 空闲退出后再次有新消息时再拉起（常驻内存由 gateway 承担）。

不做 LLM、不写业务表：那些属于 router / worker。
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional

import redis

from bus.envelope import (
    ROUTER_HEARTBEAT_KEY,
    ROUTER_SPAWNING_KEY,
    ROUTER_STREAM,
    new_event,
)
from bus.stream import Bus, try_dedupe
from channel.factory import build_channel
from config import settings
from contracts import IncomingMessage, TZ_SHANGHAI, ulid

REPO_ROOT = Path(__file__).resolve().parent
logger = logging.getLogger("gateway")

CREATE_NEW_PROCESS_GROUP = 0x00000200
MAX_CONSECUTIVE_FAILURES = 5
RETRY_BACKOFF_S = (2, 5, 10, 20, 30)


class RouterSupervisor(threading.Thread):
    """按需拉起 router 子进程，并在异常退出后有限次重试。"""

    def __init__(
        self,
        *,
        bus: Bus,
        command_client: redis.Redis,
        console: bool = False,
    ) -> None:
        super().__init__(name="router-supervisor", daemon=True)
        self._bus = bus
        self._redis = command_client
        self._console = console
        # 不能叫 `_stop`：`threading.Thread` 内部有同名方法，`join()` 会调用它。
        self._stop_event = threading.Event()
        self._proc: Optional[subprocess.Popen] = None
        self._failures = 0
        self._next_attempt = 0.0
        self.spawn_count = 0
        self.last_exit_code: Optional[int] = None

    # ------------------------------------------------------------------ 状态
    def router_alive(self) -> bool:
        return bool(self._redis.exists(ROUTER_HEARTBEAT_KEY))

    def has_work(self) -> bool:
        """组内还有未投递（lag）或未确认（PEL）的事件。"""
        try:
            return self._bus.lag() > 0 or self._bus.pel_nonempty()
        except Exception:
            logger.warning("读取 stream 状态失败", exc_info=True)
            return False

    def _log_path(self) -> Path:
        day = datetime.now(TZ_SHANGHAI).strftime("%Y-%m-%d")
        return settings.logs_dir / f"router-{day}.log"

    # ------------------------------------------------------------------ 主循环
    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception:
                logger.error("supervisor tick 失败\n%s", traceback.format_exc())
            self._stop_event.wait(1.0)

    def _tick(self) -> None:
        # 1) 回收已退出的子进程
        if self._proc is not None and self._proc.poll() is not None:
            code = self._proc.returncode
            self.last_exit_code = code
            self._proc = None
            self._redis.delete(ROUTER_SPAWNING_KEY)
            if code == 0:
                self._failures = 0
                logger.info("router 正常退出（空闲）")
            else:
                self._failures += 1
                backoff = RETRY_BACKOFF_S[min(self._failures - 1, len(RETRY_BACKOFF_S) - 1)]
                self._next_attempt = time.monotonic() + backoff
                logger.error(
                    "router 异常退出 code=%s（连续 %d 次），%.0fs 后重试；详情见 %s",
                    code,
                    self._failures,
                    backoff,
                    self._log_path(),
                )

        if self._stop_event.is_set() or self._proc is not None:
            return
        if self._failures >= MAX_CONSECUTIVE_FAILURES:
            # 连续失败多半是环境问题（凭证/DSN/模型），继续拉起只会刷日志。
            return
        if time.monotonic() < self._next_attempt:
            return
        if self.router_alive() or not self.has_work():
            return
        # 2) 抢拉起锁，避免多个 gateway 实例同时拉起
        if not self._redis.set(ROUTER_SPAWNING_KEY, "1", nx=True, ex=settings.spawning_lock_ttl_s):
            return

        # `-u` 让子进程日志实时进入文件；否则崩溃现场会因缓冲整段丢失。
        cmd = [settings.python_exe(), "-u", str(REPO_ROOT / "main_router.py")]
        if self._console:
            cmd.append("--console")
        log_path = self._log_path()
        try:
            with open(log_path, "ab") as log_file:
                self._proc = subprocess.Popen(
                    cmd,
                    cwd=str(REPO_ROOT),
                    env=os.environ.copy(),
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    creationflags=CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                )
            self.spawn_count += 1
            logger.info("已拉起 router pid=%s（第 %d 次），日志 %s", self._proc.pid, self.spawn_count, log_path)
        except Exception:
            self._redis.delete(ROUTER_SPAWNING_KEY)
            self._failures += 1
            self._next_attempt = time.monotonic() + 5
            logger.error("拉起 router 失败\n%s", traceback.format_exc())

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=3)
        proc = self._proc
        if proc is not None and proc.poll() is None:
            logger.info("停止 router pid=%s", proc.pid)
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                logger.warning("停止 router 失败", exc_info=True)
        self._redis.delete(ROUTER_SPAWNING_KEY)


def _setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [gateway] %(message)s",
        stream=sys.stdout,
    )


class Gateway:
    def __init__(self, *, console: bool) -> None:
        self._console = console
        self._redis = redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_timeout=settings.redis_socket_timeout_s,
        )
        self._bus = Bus(self._redis)
        self._bus.ensure_router_group()
        self._channel = build_channel(settings)
        self._supervisor = RouterSupervisor(
            bus=self._bus,
            command_client=self._redis,
            console=console,
        )

    # ------------------------------------------------------------------ 入队
    def enqueue(self, message: IncomingMessage) -> bool:
        """把手消息写入 stream；重复消息直接丢弃。"""
        if not try_dedupe(self._redis, message.message_id, ttl_s=settings.dedupe_ttl_s):
            logger.info("重复消息，跳过 message_id=%s", message.message_id)
            return False

        event = new_event(
            "user_message",
            message.channel,
            {
                "channel": message.channel,
                "chat_id": message.chat_id,
                "chat_type": message.chat_type,
                "message_id": message.message_id,
                "event_id": message.event_id,
                "sender_id": message.sender_id,
                "thread_id": message.thread_id,
                "text": message.text,
                "create_time": message.create_time,
            },
        )
        try:
            entry_id = self._bus.xadd(event, stream=ROUTER_STREAM)
        except Exception:
            # 入队失败必须删去重键，否则这条消息会被永久丢弃。
            self._redis.delete(f"asst:dedupe:feishu:{message.message_id}")
            logger.error("写入 stream 失败\n%s", traceback.format_exc())
            return False
        logger.info(
            "入队 message_id=%s chat=%s entry=%s text=%d 字",
            message.message_id,
            message.chat_id,
            entry_id,
            len(message.text),
        )
        return True

    def on_message(self, message: IncomingMessage) -> None:
        """渠道回调：必须毫秒级返回，异常一律吞掉（飞书会按 500 重推）。"""
        try:
            self.enqueue(message)
        except Exception:
            logger.error("处理入站消息失败\n%s", traceback.format_exc())

    # ------------------------------------------------------------------ 运行
    def run_forever(self) -> None:
        self._supervisor.start()
        try:
            self._channel.run_forever(self.on_message)
        except KeyboardInterrupt:
            logger.info("收到中断，准备退出")
        finally:
            self._supervisor.stop()

    def send_once(self, text: str, *, wait_s: float = 60.0) -> int:
        """脚本化联调：入队一条控制台消息，等 router 消费完再退出。"""
        message = IncomingMessage(
            channel="console",
            chat_id="console",
            chat_type="p2p",
            message_id=ulid(),
            event_id=ulid(),
            sender_id="console",
            text=text,
            create_time=int(time.time() * 1000),
        )
        if not self.enqueue(message):
            return 1

        self._supervisor.start()
        deadline = time.monotonic() + wait_s
        saw_router = False
        try:
            while time.monotonic() < deadline:
                if self._supervisor.spawn_count:
                    saw_router = True
                if (
                    saw_router
                    and self._supervisor._proc is None
                    and not self._supervisor.router_alive()
                    and not self._supervisor.has_work()
                ):
                    logger.info("本轮已处理完毕")
                    return 0
                time.sleep(0.5)
            logger.error("等待 router 完成超时（%.0fs）", wait_s)
            return 5
        finally:
            self._supervisor.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description="gateway 进程")
    parser.add_argument("--console", action="store_true", help="用控制台渠道（stdin/stdout）")
    parser.add_argument("--send", metavar="TEXT", help="脚本化联调：入队一条消息并等待结果后退出")
    parser.add_argument("--wait", type=float, default=60.0, help="--send 的最长等待秒数")
    args = parser.parse_args()

    if args.console or args.send:
        settings.channel_kind = "console"
    _setup_logging()
    settings.ensure_dirs()
    logger.info("启动 pid=%s channel=%s", os.getpid(), settings.channel_kind)

    gateway = Gateway(console=settings.channel_kind == "console")
    if args.send:
        return gateway.send_once(args.send, wait_s=args.wait)
    gateway.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
