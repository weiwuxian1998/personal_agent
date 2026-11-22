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
import json
import logging
import os
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import redis

from bus.envelope import (
    OUTBOX_CONSUMER,
    OUTBOX_GROUP,
    OUTBOX_STREAM,
    REPLY_IDEM_PREFIX,
    ROUTER_HEARTBEAT_KEY,
    ROUTER_SPAWNING_KEY,
    ROUTER_STREAM,
    ROUTER_STREAM_MAXLEN,
    event_from_redis,
    event_to_redis,
    new_event,
)
from bus.stream import Bus, try_dedupe
from channel.base import wrap_user_query
from channel.factory import build_channel
from config import settings
from contracts import IncomingMessage, TZ_SHANGHAI, now_utc_iso, parse_iso, ulid
from log_rotation import rotate_all
from proc_utils import pid_alive
from store.sqlite import Store
from tools.schedule import next_run_iso, render_schedule_prompt

REPO_ROOT = Path(__file__).resolve().parent
logger = logging.getLogger("gateway")

CREATE_NEW_PROCESS_GROUP = 0x00000200
MAX_CONSECUTIVE_FAILURES = 5
RETRY_BACKOFF_S = (2, 5, 10, 20, 30)

OUTBOX_SEND_RETRIES = 3

# 同一次触发（schedule_id + 当时算出的 next_run_utc）只允许被一个 gateway 实例投递。
SCHEDULE_FIRE_PREFIX = "asst:schedule:fire:"

# ReplyUser 的 products 里，这些后缀走飞书图片消息，其余走文件消息。
FEISHU_IMAGE_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".tif", ".tiff"}
)


class OutboxSender(threading.Thread):
    """消费 router 写入的 outbox，真正调用渠道发送（契约 §9.1）。

    router 进程因此不再需要 `build_channel()`，冷启动不必 `import lark_oapi`。
    发送失败重试 `OUTBOX_SEND_RETRIES` 次；仍失败则删除幂等键，并向 router
    stream 写一条 `system` 事件（`kind=send_failed`）唤醒 router 告知用户。
    """

    def __init__(self, *, channel: Any, command_client: redis.Redis, read_client: redis.Redis) -> None:
        super().__init__(name="outbox-sender", daemon=True)
        self._channel = channel
        self._redis = command_client
        self._reader = read_client
        self._stop_event = threading.Event()
        self._group_ready = False

    def _ensure_group(self) -> None:
        try:
            self._reader.xgroup_create(OUTBOX_STREAM, OUTBOX_GROUP, id="0", mkstream=True)
        except redis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def _handle(self, entry_id: str, fields: dict[str, Any]) -> None:
        raw = fields.get("data")
        event = event_from_redis({"data": raw.decode() if isinstance(raw, bytes) else str(raw)})
        payload = event.payload
        chat_id = str(payload.get("chat_id") or "")
        text = str(payload.get("text") or "")
        reply_to = str(payload.get("reply_to") or "") or None
        slot = str(payload.get("idem_slot") or "")
        products = [str(item) for item in (payload.get("products") or []) if str(item).strip()]
        key = f"{REPLY_IDEM_PREFIX}{slot}" if slot else ""

        # gateway 重启后可能重放旧 outbox：幂等键已是真实 message_id 就跳过。
        if key:
            current = self._redis.get(key)
            if current and current != "pending":
                self._reader.xack(OUTBOX_STREAM, OUTBOX_GROUP, entry_id)
                logger.info("outbox 跳过（已发送） slot=%s message_id=%s", slot, current)
                return

        failures: list[dict[str, Any]] = []

        # 1) 正文（只有文件时允许 text 为空）
        text_ok = True
        if text.strip():
            result = None
            for attempt in range(1, OUTBOX_SEND_RETRIES + 1):
                result = self._channel.send_text(
                    chat_id=chat_id,
                    text=text,
                    reply_to_message_id=reply_to,
                )
                if result.ok:
                    break
                if attempt < OUTBOX_SEND_RETRIES:
                    self._stop_event.wait(0.3 * attempt)

            text_ok = bool(result is not None and result.ok)
            if text_ok:
                if key:
                    self._redis.set(key, result.message_id, xx=True, ex=86400)
            else:
                error = (result.error if result else "no_result") or "send_failed"
                code = result.code if result else None
                failures.append({"item": "text", "error": error, "code": code})
                logger.error("outbox 正文发送失败 chat_id=%s code=%s error=%s", chat_id, code, error)

        # 2) 附件按顺序发送：单个失败只影响它自己，并降级为"把路径当文本发出去"
        for item in products:
            ok, error, code = self._send_product(chat_id=chat_id, item=item)
            if ok:
                logger.info("outbox 已发送附件 chat_id=%s item=%s", chat_id, item)
                continue
            logger.warning("outbox 附件发送失败 chat_id=%s item=%s error=%s", chat_id, item, error)
            failures.append({"item": item, "error": error, "code": code})
            try:
                self._channel.send_text(
                    chat_id=chat_id,
                    text=f"{item}（未能作为文件发送：{error}）",
                )
            except Exception:  # noqa: BLE001 - 降级提示失败也不能中断后续附件
                logger.warning("附件降级提示发送失败 item=%s", item, exc_info=True)

        # 3) 把失败回传 router，让模型决定怎么向用户解释
        if failures:
            if key:
                self._redis.delete(key)   # 允许重试，不留死键
            feedback = new_event(
                "system",
                "system",
                {
                    "kind": "send_failed" if not text_ok else "send_partial_failure",
                    "chat_id": chat_id,
                    "error": failures[0].get("error") or "send_failed",
                    "code": failures[0].get("code"),
                    "text_preview": text[:200],
                    "failures": failures,
                },
            )
            self._redis.xadd(
                ROUTER_STREAM,
                event_to_redis(feedback),
                maxlen=ROUTER_STREAM_MAXLEN,
                approximate=True,
            )
        self._reader.xack(OUTBOX_STREAM, OUTBOX_GROUP, entry_id)

    def _send_product(self, *, chat_id: str, item: str) -> tuple[bool, str, Optional[int]]:
        """发一个附件：URL → 文本链接；图片 → 图片消息；其它 → 文件消息。"""
        if item.lower().startswith(("http://", "https://")):
            result = self._channel.send_text(chat_id=chat_id, text=item)
            return result.ok, result.error, result.code
        path = Path(item)
        if not path.is_absolute():
            return False, "path_not_absolute", None
        if not chat_id:
            return False, "missing_chat_id", None
        try:
            if path.suffix.lower() in FEISHU_IMAGE_SUFFIXES:
                result = self._channel.send_image(chat_id=chat_id, path=str(path))
            else:
                result = self._channel.send_file(chat_id=chat_id, path=str(path))
        except Exception as exc:  # noqa: BLE001 - 单个附件异常不能中断整批
            return False, f"{type(exc).__name__}: {exc}", None
        return result.ok, result.error, result.code

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                if not self._group_ready:
                    self._ensure_group()
                    self._group_ready = True
                result = self._reader.xreadgroup(
                    OUTBOX_GROUP,
                    OUTBOX_CONSUMER,
                    {OUTBOX_STREAM: ">"},
                    count=10,
                    block=500,
                )
            except Exception:
                logger.warning("outbox 读取失败", exc_info=True)
                self._stop_event.wait(1.0)
                continue
            if not result:
                continue
            for entry_id, fields in result[0][1]:
                normalized = {
                    (k.decode() if isinstance(k, bytes) else str(k)): v for k, v in fields.items()
                }
                self._handle(entry_id, normalized)

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=2)


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


class LogRotator(threading.Thread):
    """常驻线程：定期给 assistant 自己的日志做大小轮转（策略见 `log_rotation.py`）。

    gateway 是唯一常驻进程，所以由它兜住 gateway/router/worker 三类日志；
    被 cmd `>>` 或子进程持有的文件走 copytruncate，不需要重启任何进程。
    """

    def __init__(self) -> None:
        super().__init__(name="log-rotator", daemon=True)
        # 不能叫 `_stop`：`threading.Thread` 内部有同名方法。
        self._stop_event = threading.Event()

    def run(self) -> None:
        interval = max(int(settings.log_rotate_check_s), 5)
        while not self._stop_event.is_set():
            try:
                for path, action, before in rotate_all(in_process=True):
                    logger.warning("日志轮转 %s（%d 字节）-> %s", path, before, action)
            except Exception:
                logger.warning("日志轮转失败", exc_info=True)
            self._stop_event.wait(interval)

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=2)


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
        self._outbox_reader = redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_timeout=None,   # 阻塞读不能用 socket 超时
        )
        self._sender = OutboxSender(
            channel=self._channel,
            command_client=self._redis,
            read_client=self._outbox_reader,
        )
        self._supervisor = RouterSupervisor(
            bus=self._bus,
            command_client=self._redis,
            console=console,
        )
        self._store = Store(settings.db_path)
        self._store.init_schema()
        self._watchdog = TaskWatchdog(command_client=self._redis, store=self._store)
        self._scanner = ScheduleScanner(command_client=self._redis, store=self._store)
        self._log_rotator = LogRotator()

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
                # 用户输入固定包装（原话 + UTC+8 时间 + 附件路径）；在进入 router 之前完成，
                # 因此 JSONL 落盘的也是这份文本。
                "text": wrap_user_query(
                    message.text,
                    create_time_ms=message.create_time,
                    attachments=message.attachments,
                ),
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
        self._sender.start()
        self._supervisor.start()
        self._watchdog.start()
        self._scanner.start()
        if settings.log_rotate_enabled:
            self._log_rotator.start()
        try:
            self._channel.run_forever(self.on_message)
        except KeyboardInterrupt:
            logger.info("收到中断，准备退出")
        finally:
            if settings.log_rotate_enabled:
                self._log_rotator.stop()
            self._scanner.stop()
            self._watchdog.stop()
            self._supervisor.stop()
            self._sender.stop()

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

        self._sender.start()
        self._supervisor.start()
        self._watchdog.start()
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
                    and self._outbox_pending() == 0
                ):
                    logger.info("本轮已处理完毕")
                    return 0
                time.sleep(0.5)
            logger.error("等待 router 完成超时（%.0fs）", wait_s)
            return 5
        finally:
            self._watchdog.stop()
            self._supervisor.stop()
            self._sender.stop()

    def _outbox_pending(self) -> int:
        """outbox 里尚未发送的事件数（lag + 未 ACK）。"""
        try:
            for group in self._outbox_reader.xinfo_groups(OUTBOX_STREAM) or []:
                info = {
                    (k.decode() if isinstance(k, bytes) else str(k)): v for k, v in group.items()
                }
                if info.get("name") == OUTBOX_GROUP:
                    return int(info.get("lag") or 0) + int(info.get("pending") or 0)
        except redis.ResponseError:
            return 0
        return 0


class ScheduleScanner(threading.Thread):
    """定时任务扫描（契约 §9.3：每 `schedule_scan_interval_s` 扫一次）。

    到期行按 `dispatch` 分两条路：

    - `router`（默认）：投一条 `timer` 事件进 router stream，唤醒 router 自己决定做什么；
    - `worker`：gateway 直接 `create_session(task_kind=...)` 派 worker，跳过 router 的
      LLM 往返（记忆整理这类"动作固定"的活走这条）；worker 跑完自己把 `worker_result`
      投回 router stream，router 侧记账/汇报照旧，不需要 gateway 中转结果。

    两条路都会推进 `next_run_utc`。多 gateway 实例靠 `SET NX` 抢同一次触发（键含
    `schedule_id + next_run_utc`）；投递或派发失败会删键并保留 `next_run_utc`，下一轮重试。
    """

    def __init__(self, *, command_client: redis.Redis, store: Store) -> None:
        super().__init__(name="schedule-scanner", daemon=True)
        self._redis = command_client
        self._store = store
        self._stop_event = threading.Event()
        # SessionService 首次直发时才构造：它会 import langchain，不该拖慢 gateway 冷启动。
        self._sessions: Any = None

    def _session_service(self) -> Any:
        if self._sessions is None:
            from tools.sessions import SessionService

            self._sessions = SessionService(
                store=self._store,
                bus=Bus(self._redis),
                redis_client=self._redis,
                settings=settings,
                repo_root=Path(__file__).resolve().parent,
                checkpointer=None,   # 直发只负责创建，不读 checkpoint
            )
        return self._sessions

    def _fire_worker(
        self, record: Any, *, now_dt: datetime, next_iso: str | None, key: str
    ) -> None:
        """dispatch=worker：直接派 worker，不经过 router。失败则删锁等下一轮。"""
        kind = (record.task_kind or "").strip()
        if not kind:
            logger.error("调度 %s 直发 worker 但缺少 task_kind，本轮跳过", record.id)
            self._store.update_schedule_after_run(
                record.id, next_run_utc=next_iso, ran_at_utc=now_utc_iso()
            )
            return
        try:
            service = self._session_service()
        except Exception:
            self._redis.delete(key)
            logger.error("调度 %s 直发 worker 失败：SessionService 不可用", record.id, exc_info=True)
            return
        prompt = render_schedule_prompt(record.prompt, at=now_dt)
        title = render_schedule_prompt(record.title, at=now_dt) if record.title else None
        try:
            result = service.create_session(prompt, title=title, task_kind=kind)
        except Exception as exc:
            self._redis.delete(key)
            logger.error("调度 %s 直发 worker 异常：%s", record.id, exc, exc_info=True)
            return
        if not result.get("ok"):
            self._redis.delete(key)
            logger.error(
                "调度 %s 直发 worker 失败：%s", record.id, result.get("message") or result
            )
            return
        self._store.update_schedule_after_run(
            record.id, next_run_utc=next_iso, ran_at_utc=now_utc_iso()
        )
        logger.info(
            "调度触发 %s → 直发 worker task=%s kind=%s；下一次：%s",
            record.id, result.get("task_id"), kind, next_iso or "无（一次性任务完成）",
        )

    def _tick(self) -> None:
        now = now_utc_iso()
        now_dt = parse_iso(now)
        for record in self._store.list_due_schedules(now, limit=20):
            key = f"{SCHEDULE_FIRE_PREFIX}{record.id}:{record.next_run_utc}"
            if not self._redis.set(key, "1", nx=True, ex=86400):
                continue   # 另一个 gateway 已经抢到这次触发
            late_s = (now_dt - parse_iso(record.next_run_utc or now)).total_seconds()
            next_iso = next_run_iso(record, after=now_dt)
            if late_s > record.misfire_grace_s and record.catch_up != "once":
                logger.warning(
                    "调度 %s 迟到 %.0fs 超过宽限 %ss，跳过本次触发（下一次 %s）",
                    record.id, late_s, record.misfire_grace_s, next_iso or "无",
                )
                self._store.update_schedule_after_run(record.id, next_run_utc=next_iso, ran_at_utc=now)
                continue
            if str(record.dispatch or "router").strip().lower() == "worker":
                self._fire_worker(record, now_dt=now_dt, next_iso=next_iso, key=key)
                continue
            event = new_event(
                "timer",
                "scheduler",
                {
                    "schedule_id": record.id,
                    "scheduled_at": record.next_run_utc or "",
                    "prompt": record.prompt,
                },
            )
            try:
                self._redis.xadd(
                    ROUTER_STREAM,
                    event_to_redis(event),
                    maxlen=ROUTER_STREAM_MAXLEN,
                    approximate=True,
                )
            except Exception:
                self._redis.delete(key)
                logger.error("调度 %s 投递失败，保留到期时间等下一轮重试", record.id, exc_info=True)
                continue
            self._store.update_schedule_after_run(record.id, next_run_utc=next_iso, ran_at_utc=now)
            logger.info(
                "调度触发 %s（迟到 %.0fs）→ timer 事件；下一次：%s",
                record.id, late_s, next_iso or "无（一次性任务完成）",
            )

    def run(self) -> None:
        interval = max(1, int(settings.schedule_scan_interval_s))
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception:
                logger.warning("调度扫描失败", exc_info=True)
            self._stop_event.wait(interval)

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=2)


class TaskWatchdog(threading.Thread):
    """worker 巡检（设计文档 §5.7.3）。

    每 `watch_scan_interval_s` 扫一次 `status='running'` 的会话：

    - 进程不在 → `process_gone`；
    - 进程在但 `last_seen` 超过 `stall_minutes` → `stalled`。

    命中后写 `task_watch`（主键去重，只有 `first` 才提醒），再往 router stream
    写一条 `system/kind=task_watch` 事件，让 router 去问用户"重试还是放弃"。
    **绝不自动重派**，也不直接给用户发消息（用户出口仍是 ReplyUser）。
    """

    def __init__(self, *, command_client: redis.Redis, store: Store) -> None:
        super().__init__(name="task-watchdog", daemon=True)
        self._redis = command_client
        self._store = store
        self._stop_event = threading.Event()

    def _task_chat_id(self, task_id: str) -> str:
        path = settings.workers_dir / task_id / "task.json"
        try:
            return str(json.loads(path.read_text(encoding="utf-8")).get("chat_id") or "")
        except Exception:  # noqa: BLE001 - 任务目录可能还没落盘
            return ""

    def _detect(self, session: Any) -> str:
        if not pid_alive(session.pid):
            return "process_gone"
        beat = session.last_seen or session.updated_at
        try:
            beat_dt = datetime.fromisoformat(str(beat))
        except (TypeError, ValueError):
            return ""
        if beat_dt.tzinfo is None:
            beat_dt = beat_dt.replace(tzinfo=TZ_SHANGHAI)
        stalled_s = (datetime.now(timezone.utc) - beat_dt.astimezone(timezone.utc)).total_seconds()
        return "stalled" if stalled_s > settings.stall_minutes * 60 else ""

    def _tick(self) -> None:
        for session in self._store.list_sessions(status="running", limit=200):
            reason = self._detect(session)
            if not reason:
                continue
            if self._store.mark_watch(session.id, reason) != "first":
                continue
            task_id = session.id.split(":", 1)[1] if ":" in session.id else session.id
            chat_id = self._task_chat_id(task_id)
            text = (
                f"任务 {task_id} 疑似卡住（原因：{reason}）。"
                "请询问用户：要重试还是放弃？不要自动重派。"
            )
            event = new_event(
                "system",
                "system",
                {
                    "kind": "task_watch",
                    "task_id": task_id,
                    "reason": reason,
                    "chat_id": chat_id,
                    "text": text,
                },
                trace_id=session.id,
            )
            self._redis.xadd(
                ROUTER_STREAM,
                event_to_redis(event),
                maxlen=ROUTER_STREAM_MAXLEN,
                approximate=True,
            )
            logger.warning("巡检提醒 task=%s reason=%s chat=%s", task_id, reason, chat_id)

    def run(self) -> None:
        interval = max(5, int(settings.watch_scan_interval_s))
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception:
                logger.warning("巡检失败", exc_info=True)
            self._stop_event.wait(interval)

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=2)


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
