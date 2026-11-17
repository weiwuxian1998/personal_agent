"""Router 进程入口：按需拉起、空闲退出、消费者组批量消费（契约 §9.2）。

生命周期：gateway 在「有未投递事件且没有心跳」时拉起本进程；本进程处理完积压，
连续 `router_idle_exit_s` 秒没有新事件且 PEL 为空时自行退出（退出码 0），
下一次有消息再由 gateway 拉起。崩溃时（退出码非 0）未 ACK 的事件留在 PEL，
下次启动通过 XAUTOCLAIM 认领重放。

本文件只做编排，不实现业务：渠道在 `channel/`（只由 gateway 使用，本进程不 import）、
工具在 `tools/`、装配在 `agents/router/graph.py`。回复统一写入 outbox Stream，
由 gateway 发送（契约 §9.1），因此这里不构造渠道、也不 import `lark_oapi`。
"""

from __future__ import annotations

import argparse
import hashlib
import json
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

from agents.base import last_ai_text, stream_agent_steps
from agents.context import (
    current_chat_id,
    current_chat_type,
    current_message_id,
    current_trace_id,
)
from agents.middleware import reset_tool_ms, take_pending_compact, take_tool_ms
from agents.router.graph import build_router
from bus.envelope import (
    REPLY_IDEM_PREFIX,
    ROUTER_HEARTBEAT_KEY,
    ROUTER_STATE_KEY,
    ROUTER_STREAM,
    render_digest,
)
from bus.stream import Bus
from config import settings
from contracts import Event, now_iso, ulid
from memory.jsonl import JsonlWriter
from memory.ledger import (
    append_compact,
    append_consolidated,
    consolidation_report_path,
    current_summary_ref,
    load_state,
)
from store.checkpointer import open_checkpointer
from store.sqlite import Store
from tools.sessions import SessionService

REPO_ROOT = Path(__file__).resolve().parent
logger = logging.getLogger("router")

# 崩溃后认领 PEL 的等待时间：给「上一个 router 可能还活着」留出余量。
CLAIM_MIN_IDLE_MS = 5000

# 回复 message_id 回填：ReplyUser 只入队就返回，真实 message_id 由 gateway 发送后写回
# 幂等键（契约 §9.1.1）。这里在写 turns.jsonl 前做有界等待，拿不到就如实留空。
REPLY_ID_WAIT_S = 2.0
REPLY_ID_POLL_S = 0.05


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
    # HTTP 客户端的逐请求 INFO 噪音降级；模型/工具的关键过程由 stream_agent_steps 落日志。
    for noisy in ("httpx", "httpcore", "openai", "langchain_openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _latest_user_chat(events: list[Event]) -> tuple[str, str]:
    """从一批事件里取最后一条用户消息的 chat_id / chat_type。"""
    chat_id, chat_type = "", "p2p"
    for event in events:
        payload = event.payload
        if event.type == "user_message":
            chat_id = str(payload.get("chat_id") or chat_id)
            chat_type = str(payload.get("chat_type") or chat_type)
        elif event.type in ("worker_result", "worker_message", "worker_notice", "system"):
            # worker_result / SendMessage / send_failed 的批次里没有 user_message：会话归属由
            # 任务带回（contracts.TaskSpec.chat_id），不能用全局 last_chat_id。
            chat_id = str(payload.get("chat_id") or chat_id)
    return chat_id, chat_type


def _latest_user_message_id(events: list[Event]) -> str:
    """本批最后一条用户消息的 message_id。

    ReplyUser 的 `reply_to=None` 默认引用它；定时任务 / worker 回报等没有原始
    用户消息的批次返回空串，避免硬编一个 reply_to。
    """
    for event in reversed(events):
        if event.type == "user_message":
            return str(event.payload.get("message_id") or "")
    return ""


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
        if payload.get("task_kind") == "memory_consolidate" and status == "done":
            _append_consolidated(str(payload.get("task_id") or ""))


def _append_consolidated(task_id: str) -> None:
    """记忆整理完成后回写 `consolidated` 记账（JSONL 只由 router 追加）。"""
    try:
        report_path = consolidation_report_path(settings.memory_dir)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        covers = report.get("covers_seq") or []
        version = int(report.get("summary_version") or 0)
        seq = append_consolidated(
            settings.memory_dir,
            task_id=task_id,
            covers_seq=covers if isinstance(covers, list) else [],
            summary_ref=f"memory/summary.md#v{version}" if version else "",
            summary_version=version,
            facts_written=int(report.get("facts_written") or 0),
            decisions_written=int(report.get("decisions_written") or 0),
        )
        logger.info("追加 consolidated 记账 seq=%s 覆盖=%s 摘要 v%s", seq, covers, version)
    except FileNotFoundError:
        logger.warning("记忆整理任务 %s 完成但没有 last-consolidation.json，未写 consolidated 记账", task_id)
    except Exception:
        logger.warning("写 consolidated 记账失败 task=%s", task_id, exc_info=True)


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


def _reply_products(messages: list[Any]) -> list[str]:
    """取本批 ReplyUser 调用里带过的附件列表，落进 turns.jsonl 便于事后核对。"""
    products: list[str] = []
    for message in messages:
        for call in getattr(message, "tool_calls", None) or []:
            if isinstance(call, dict):
                name, args = call.get("name"), call.get("args")
            else:
                name, args = getattr(call, "name", ""), getattr(call, "args", None)
            if name != "ReplyUser" or not isinstance(args, dict):
                continue
            for item in args.get("products") or []:
                products.append(str(item))
    return products


def _wake_entries(events: list[Event]) -> list[dict[str, Any]]:
    """把唤醒本轮的事件压成 turns.jsonl 的 `wake` 列表（架构 §3.3）。"""
    entries: list[dict[str, Any]] = []
    for event in events:
        payload = event.payload or {}
        entry: dict[str, Any] = {"type": event.type}
        if event.type == "user_message":
            entry["message_id"] = str(payload.get("message_id") or "")
            entry["text"] = str(payload.get("text") or "")
        elif event.type in ("worker_result", "worker_message", "worker_notice"):
            entry["task_id"] = str(payload.get("task_id") or "")
            entry["text"] = str(payload.get("text") or payload.get("detail") or "")
        elif event.type == "timer":
            entry["schedule_id"] = str(payload.get("schedule_id") or "")
            entry["text"] = str(payload.get("prompt") or "")
        elif event.type == "system":
            entry["kind"] = str(payload.get("kind") or "")
            if payload.get("text"):
                entry["text"] = str(payload["text"])
        elif event.type == "router_message":
            entry["text"] = str(payload.get("text") or "")
        entries.append(entry)
    return entries


def _iter_tool_calls(messages: list[Any]):
    """按顺序产出消息里的工具调用 `(tool_call_id, name, args)`。"""
    for message in messages:
        for call in getattr(message, "tool_calls", None) or []:
            if isinstance(call, dict):
                yield (
                    str(call.get("id") or ""),
                    str(call.get("name") or "?"),
                    call.get("args"),
                )
            else:
                yield (
                    str(getattr(call, "id", "") or ""),
                    str(getattr(call, "name", "?")),
                    getattr(call, "args", None),
                )


def _tool_results(messages: list[Any]) -> dict[str, Any]:
    """`tool_call_id` → ToolMessage（用于取 ok 状态与工具返回体）。"""
    results: dict[str, Any] = {}
    for message in messages:
        if getattr(message, "type", "") != "tool":
            continue
        call_id = str(getattr(message, "tool_call_id", "") or "")
        if call_id:
            results[call_id] = message
    return results


def _tool_result_payload(message: Any) -> dict[str, Any]:
    """工具返回的 dict（LangChain 会序列化成 JSON 字符串放在 content 里）。"""
    content = getattr(message, "content", "")
    if isinstance(content, list):
        content = "\n".join(
            str(block.get("text", "")) if isinstance(block, dict) else str(block)
            for block in content
        )
    if isinstance(content, dict):
        return content
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _args_hash(args: Any) -> str:
    """参数哈希：稳定键序的 sha256 前 16 位（与 sqlite 的 args_hash 同风格）。"""
    try:
        raw = json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        raw = str(args)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _turn_tool_calls(messages: list[Any], tool_ms: dict[str, int]) -> list[dict[str, Any]]:
    """本轮全部工具调用：名称 / 参数哈希 / 是否成功 / 实测耗时（ms）。"""
    results = _tool_results(messages)
    calls: list[dict[str, Any]] = []
    for call_id, name, args in _iter_tool_calls(messages):
        result = results.get(call_id)
        ok = False
        if result is not None:
            payload = _tool_result_payload(result)
            status_ok = str(getattr(result, "status", "") or "") != "error"
            ok = (bool(payload["ok"]) if "ok" in payload else status_ok) and status_ok
        calls.append(
            {
                "name": name,
                "args_hash": _args_hash(args),
                "ok": ok,
                "ms": int(tool_ms.get(call_id) or 0),
            }
        )
    return calls


def _collect_reply_calls(messages: list[Any], trigger_message_id: str) -> list[dict[str, Any]]:
    """收集本轮 ReplyUser 调用（text / reply_to / 幂等槽位，message_id 稍后回填）。"""
    results = _tool_results(messages)
    calls: list[dict[str, Any]] = []
    for call_id, name, args in _iter_tool_calls(messages):
        if name != "ReplyUser":
            continue
        data = args if isinstance(args, dict) else {}
        payload = _tool_result_payload(results[call_id]) if call_id in results else {}
        if payload and payload.get("ok") is False:
            # 入队失败 / 空文本：没有真正发出，不计入 replies。
            continue
        reply_to = data.get("reply_to")
        # 与 tools/reply.py 保持同一语义：None → 引用触发消息；"" → 不引用。
        resolved_reply_to = trigger_message_id if reply_to is None else str(reply_to)
        calls.append(
            {
                "text": str(data.get("text") or ""),
                "message_id": str(payload.get("message_id") or "") if payload.get("deduped") else "",
                "reply_to": resolved_reply_to,
                "slot": str(payload.get("slot") or ""),
            }
        )
    return calls


def _resolve_reply_message_ids(client: redis.Redis, calls: list[dict[str, Any]]) -> None:
    """把 gateway 发送后回填到幂等键的真实 message_id 取回来（有界等待）。

    ReplyUser 只负责入队，真实 message_id 由 gateway 的 OutboxSender 在发送成功后
    写入 `asst:reply:idem:<slot>`（契约 §9.1.1）。等不到（gateway 未运行 / 仍在排队）
    就留空，不伪造。
    """
    pending_slots = [item["slot"] for item in calls if item["slot"] and not item["message_id"]]
    if not pending_slots:
        return
    deadline = time.monotonic() + REPLY_ID_WAIT_S
    while pending_slots and time.monotonic() < deadline:
        waiting: list[str] = []
        for slot in pending_slots:
            try:
                value = client.get(f"{REPLY_IDEM_PREFIX}{slot}")
            except Exception:
                logger.warning("读取回复 message_id 失败 slot=%s", slot, exc_info=True)
                value = None
            if value and value != "pending":
                for item in calls:
                    if item["slot"] == slot:
                        item["message_id"] = str(value)
            else:
                waiting.append(slot)
        pending_slots = waiting
        if pending_slots:
            time.sleep(REPLY_ID_POLL_S)
    if pending_slots:
        logger.info("回复仍在排队，turns.jsonl 的 message_id 留空 slots=%s", pending_slots)


def _turn_usage(messages: list[Any]) -> dict[str, int]:
    """汇总本轮所有模型调用的真实 usage（reasoning / cache 取模型返回的明细）。"""
    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
        "reasoning_tokens": 0,
    }
    for message in messages:
        data = getattr(message, "usage_metadata", None)
        if not isinstance(data, dict):
            continue
        usage["input_tokens"] += int(data.get("input_tokens") or 0)
        usage["output_tokens"] += int(data.get("output_tokens") or 0)
        usage["total_tokens"] += int(data.get("total_tokens") or 0)
        usage["cached_tokens"] += int((data.get("input_token_details") or {}).get("cache_read") or 0)
        usage["reasoning_tokens"] += int(
            (data.get("output_token_details") or {}).get("reasoning") or 0
        )
    return usage


def _turn_context(pending: dict[str, Any] | None) -> dict[str, Any]:
    """本轮上下文状态（不含 dropped_messages，按用户要求）。"""
    try:
        memory_version = int(load_state(settings.memory_dir).get("summary_version") or 0)
    except Exception:
        memory_version = 0
    context: dict[str, Any] = {
        "memory_version": memory_version,
        "compacted_messages": int((pending or {}).get("removed") or 0),
    }
    if pending:
        context["tokens_before"] = int(pending.get("tokens_before") or 0)
        context["tokens_after"] = int(pending.get("tokens_after") or 0)
    return context


def _current_checkpoint_id(graph: Any) -> str:
    """本轮结束时的 checkpoint id（与 JSONL 的 boundary 对账用）；不可用时为空串。"""
    try:
        snapshot = graph.get_state({"configurable": {"thread_id": settings.router_thread_id}})
        config = getattr(snapshot, "config", None) or {}
        return str(((config or {}).get("configurable") or {}).get("checkpoint_id") or "")
    except Exception:
        return ""


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
    # ReplyUser 的 reply_to=None 默认引用本批最后一条用户消息；没有用户消息时为空。
    trigger_message_id = _latest_user_message_id(events)
    current_message_id.set(trigger_message_id)
    trace_id = next((e.trace_id for e in events if e.type == "user_message"), "") or ulid()
    current_trace_id.set(trace_id)
    turn_id = ulid()

    digest = render_digest(events)
    reset_tool_ms()
    # stream 而非 invoke：模型思考、工具调用及结果逐步实时落日志。
    messages = stream_agent_steps(
        graph,
        {"messages": [{"role": "user", "content": digest}]},
        {
            "configurable": {"thread_id": settings.router_thread_id},
            "recursion_limit": settings.router_recursion_limit,
        },
        logger=logger,
    )
    tool_ms = take_tool_ms()
    reply = last_ai_text(messages)
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

    pending = take_pending_compact()
    reply_calls = _collect_reply_calls(messages, trigger_message_id)
    _resolve_reply_message_ids(command_client, reply_calls)
    replies = [
        {
            "text": item["text"],
            "message_id": item["message_id"],
            "reply_to": item["reply_to"],
        }
        for item in reply_calls
    ]

    # compact 的 covers_seq 只覆盖「本轮之前」已落盘的记录：本轮 digest 仍在上下文里。
    last_seq_before = turns.next_seq() - 1
    turns.append(
        {
            "turn_id": turn_id,
            "checkpoint_id": _current_checkpoint_id(graph),
            "trace_id": trace_id,
            "chat_id": chat_id,
            "wake": _wake_entries(events),
            "event_ids": [e.id for e in events],
            "digest": digest,
            "assistant_text": reply,
            "reply": reply,
            "replies": replies,
            "tool_calls": _turn_tool_calls(messages, tool_ms),
            "products": _reply_products(messages),
            "usage": _turn_usage(messages),
            "context": _turn_context(pending),
            "ok": True,
        }
    )
    if pending:
        try:
            compact_seq = append_compact(
                settings.memory_dir,
                reason=str(pending.get("reason") or ""),
                boundary_checkpoint_id=str(pending.get("boundary") or ""),
                removed_messages=int(pending.get("removed") or 0),
                tokens_before=int(pending.get("tokens_before") or 0),
                tokens_after=int(pending.get("tokens_after") or 0),
                upto_seq=last_seq_before,
                summary_ref=current_summary_ref(settings.memory_dir),
            )
            logger.info(
                "追加 compact 记账 seq=%s 覆盖=1..%s 原因=%s boundary=%s",
                compact_seq, last_seq_before, pending.get("reason"), pending.get("boundary"),
            )
        except Exception:
            logger.warning("写 compact 记账失败（不影响本轮）", exc_info=True)
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
            outbox=bus,
            redis_client=command_client,
            # read_session / TaskOutput 读的就是这个 saver（worker 的 checkpoint 同库不同 thread）。
            session_service=SessionService(
                store=store,
                bus=bus,
                redis_client=command_client,
                settings=settings,
                repo_root=REPO_ROOT,
                checkpointer=saver,
            ),
            checkpointer=saver,
            store=store,
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
