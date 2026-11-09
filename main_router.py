"""Router 进程入口：按需拉起、空闲退出、消费者组批量消费（契约 §9.2）。

生命周期：gateway 在「有未投递事件且没有心跳」时拉起本进程；本进程处理完积压，
连续 `router_idle_exit_s` 秒没有新事件且 PEL 为空时自行退出（退出码 0），
下一次有消息再由 gateway 拉起。崩溃时（退出码非 0）未 ACK 的事件留在 PEL，
下次启动通过 XAUTOCLAIM 认领重放。

本文件只做编排，不实现业务：渠道在 `channel/`、工具在 `tools/`、
装配在 `agents/router/graph.py`。
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any

import redis

from agents.base import last_ai_text
from agents.context import current_chat_id, current_chat_type, current_trace_id
from agents.router.graph import build_router
from bus.envelope import (
    ROUTER_HEARTBEAT_KEY,
    ROUTER_STATE_KEY,
    ROUTER_STREAM,
    render_digest,
)
from bus.stream import Bus
from channel.factory import build_channel
from config import settings
from contracts import Event, now_iso, ulid
from memory.jsonl import JsonlWriter
from store.checkpointer import open_checkpointer
from store.sqlite import Store
from tools.sessions import SessionService

REPO_ROOT = Path(__file__).resolve().parent
logger = logging.getLogger("router")

# 崩溃后认领 PEL 的等待时间：给「上一个 router 可能还活着」留出余量。
CLAIM_MIN_IDLE_MS = 5000


class Heartbeat(threading.Thread):
    """定期续期心跳键；gateway 靠它判断 router 是否存活。"""

    def __init__(self, client: redis.Redis, ttl_s: int) -> None:
        super().__init__(name="router-heartbeat", daemon=True)
        self._client = client
        self._ttl = max(int(ttl_s), 1)
        # 注意：不能叫 `_stop`——`threading.Thread` 内部有同名方法，`join()` 会调用它。
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._client.set(
                    ROUTER_HEARTBEAT_KEY,
                    f"{socket.gethostname()}:{os.getpid()}",
                    ex=self._ttl,
                )
            except Exception:
                logger.warning("心跳续期失败", exc_info=True)
            self._stop_event.wait(self._ttl / 2)

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=2)


def _setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [router] %(message)s",
        stream=sys.stdout,
    )


def _latest_user_chat(events: list[Event]) -> tuple[str, str]:
    """从一批事件里取最后一条用户消息的 chat_id / chat_type。"""
    chat_id, chat_type = "", "p2p"
    for event in events:
        if event.type == "user_message":
            payload = event.payload
            chat_id = str(payload.get("chat_id") or chat_id)
            chat_type = str(payload.get("chat_type") or chat_type)
    return chat_id, chat_type


def _apply_worker_results(store: Store, events: list[Event]) -> None:
    """worker_result 回来时更新会话行（这是 router 的职责，worker 不写业务状态）。"""
    for event in events:
        if event.type != "worker_result":
            continue
        payload = event.payload
        session_id = str(payload.get("session_id") or f"worker:{payload.get('task_id', '')}")
        status = str(payload.get("status") or "done")
        try:
            session = store.get_session(session_id)
            if session:
                store.set_status(session_id, status)
                if session.first_message_at is None:
                    store.set_first_message_at(session_id, event.ts)
        except Exception:
            logger.warning("更新会话状态失败 session=%s", session_id, exc_info=True)


def _tool_call_names(messages: list[Any]) -> list[str]:
    """按顺序取出本批消息里所有工具调用的名字（用于日志与 turns 记账）。"""
    names: list[str] = []
    for message in messages:
        for call in getattr(message, "tool_calls", None) or []:
            if isinstance(call, dict):
                names.append(str(call.get("name") or "?"))
            else:
                names.append(str(getattr(call, "name", "?")))
    return names


def _process_batch(
    *,
    batch: list[tuple[str, Event]],
    bus: Bus,
    graph: Any,
    store: Store,
    command_client: redis.Redis,
    inbox: JsonlWriter,
    turns: JsonlWriter,
) -> bool:
    """处理一批事件并 ACK；返回是否调用了 ReplyUser（仅用于日志/观测）。"""
    events = [event for _entry_id, event in batch]

    for event in events:
        inbox.append(
            {
                "event_id": event.id,
                "type": event.type,
                "source": event.source,
                "trace_id": event.trace_id,
                "payload": event.payload,
                "ts": event.ts,
                "entry_ts": now_iso(),
            }
        )
    _apply_worker_results(store, events)

    chat_id, chat_type = _latest_user_chat(events)
    if chat_id:
        command_client.hset(
            ROUTER_STATE_KEY,
            mapping={"last_chat_id": chat_id, "last_chat_type": chat_type, "updated_at": now_iso()},
        )
    current_chat_id.set(chat_id)
    current_chat_type.set(chat_type)
    trace_id = next((e.trace_id for e in events if e.type == "user_message"), "") or ulid()
    current_trace_id.set(trace_id)

    digest = render_digest(events)
    result = graph.invoke(
        {"messages": [{"role": "user", "content": digest}]},
        {
            "configurable": {"thread_id": settings.router_thread_id},
            "recursion_limit": settings.router_recursion_limit,
        },
    )
    reply = last_ai_text(result.get("messages") or [])
    messages = result.get("messages") or []
    tool_names = _tool_call_names(messages)
    logger.info(
        "模型产出：消息 %d 条，工具调用 %d 次 %s；回复 %d 字",
        len(messages),
        len(tool_names),
        tool_names or "[]",
        len(reply),
    )
    if reply:
        logger.info("模型回复原文：%s", reply[:800])

    turns.append(
        {
            "trace_id": trace_id,
            "chat_id": chat_id,
            "event_ids": [e.id for e in events],
            "digest": digest,
            "reply": reply,
            "tool_calls": tool_names,
            "ok": True,
        }
    )
    bus.ack(batch)
    return bool(reply)


def main() -> int:
    parser = argparse.ArgumentParser(description="router 进程（按需拉起）")
    parser.add_argument("--console", action="store_true", help="用控制台渠道（不连飞书）")
    parser.add_argument("--once", action="store_true", help="处理一批事件后退出（联调用）")
    args = parser.parse_args()

    if args.console:
        settings.channel_kind = "console"
    _setup_logging()
    settings.ensure_dirs()
    pid = os.getpid()
    logger.info(
        "启动 pid=%s channel=%s idle_exit=%ss stream=%s",
        pid,
        settings.channel_kind,
        settings.router_idle_exit_s,
        ROUTER_STREAM,
    )

    command_client = redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_timeout=settings.redis_socket_timeout_s,
    )
    # 阻塞读必须用无 socket 超时的连接，否则 block 期间会被客户端判超时。
    read_client = redis.from_url(settings.redis_url, decode_responses=True, socket_timeout=None)
    bus = Bus(read_client)
    bus.ensure_router_group()

    heartbeat = Heartbeat(command_client, settings.heartbeat_ttl_s)
    heartbeat.start()

    store = Store(settings.db_path)
    store.init_schema()
    channel = build_channel(settings)
    service = SessionService(
        store=store,
        bus=bus,
        redis_client=command_client,
        settings=settings,
        repo_root=REPO_ROOT,
    )
    inbox = JsonlWriter(settings.memory_dir, "inbox")
    turns = JsonlWriter(settings.memory_dir, "turns")

    exit_code = 0
    checkpointer_ctx = None
    try:
        try:
            if settings.postgres_dsn:
                checkpointer_ctx = open_checkpointer(
                    settings.pg_dsn(),
                    min_size=settings.postgres_pool_min,
                    max_size=settings.postgres_pool_max,
                )
                _pool, saver = checkpointer_ctx.__enter__()
            else:
                saver = None
                logger.warning("未配置 POSTGRES_DSN：router 以无 checkpoint 模式运行（上下文不跨轮保留）")
        except Exception:
            logger.warning("Postgres checkpointer 不可用，降级为无 checkpoint\n%s", traceback.format_exc())
            saver = None

        graph = build_router(
            settings=settings,
            channel=channel,
            redis_client=command_client,
            session_service=service,
            checkpointer=saver,
            repo_root=REPO_ROOT,
        )

        last_active = time.monotonic()
        pending = bus.claim_pending(min_idle_ms=CLAIM_MIN_IDLE_MS, count=settings.batch_max)
        if pending:
            logger.info("启动时认领 PEL 遗留事件 %d 条", len(pending))

        while True:
            batch = pending
            pending = []
            if not batch:
                batch = bus.read_batch(
                    block_ms=settings.router_read_block_ms,
                    count=settings.batch_max,
                )
            if not batch:
                if bus.pel_nonempty():
                    batch = bus.claim_pending(
                        min_idle_ms=CLAIM_MIN_IDLE_MS,
                        count=settings.batch_max,
                    )
                if not batch:
                    idle = time.monotonic() - last_active
                    # lag 是「组内尚未投递的事件数」：必须为 0 才能安全退出，
                    # 否则会漏掉刚到达、还未被 XREADGROUP 取走的消息。
                    if bus.lag() == 0 and idle >= settings.router_idle_exit_s:
                        logger.info("空闲 %.0fs 且无待处理事件，退出", idle)
                        break
                    continue

            batch = bus.drain_more(
                batch,
                debounce_ms=settings.batch_debounce_ms,
                max_batch=settings.batch_max,
            )
            types = ",".join(event.type for _id, event in batch)
            logger.info("处理批次 %d 条：%s", len(batch), types)
            replied = _process_batch(
                batch=batch,
                bus=bus,
                graph=graph,
                store=store,
                command_client=command_client,
                inbox=inbox,
                turns=turns,
            )
            if not replied:
                logger.warning("本轮没有 ReplyUser 调用（模型未回复用户）")
            last_active = time.monotonic()
            if args.once:
                logger.info("--once：本批处理完成后退出")
                break
    except Exception:
        exit_code = 2
        logger.error("router 处理失败；未 ACK 的事件留在 PEL 等下次认领\n%s", traceback.format_exc())
        try:
            command_client.hset(
                ROUTER_STATE_KEY,
                mapping={"last_error": traceback.format_exc(limit=1).strip(), "updated_at": now_iso()},
            )
        except Exception:
            pass
    finally:
        heartbeat.stop()
        try:
            command_client.delete(ROUTER_HEARTBEAT_KEY)
        except Exception:
            pass
        if checkpointer_ctx is not None:
            checkpointer_ctx.__exit__(None, None, None)
        logger.info("router 退出 pid=%s code=%s", pid, exit_code)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
