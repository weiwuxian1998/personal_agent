"""Redis 键名、Stream 协议与事件信封的读写工具。

**本文件由主线维护**（所有模块都 import 它，改动必须走契约变更流程）。
数据结构的定义在 `types.py`；这里只放常量与纯函数，不 import redis。
"""

from __future__ import annotations

from typing import Any, Iterable

from contracts import Event, now_iso

# ----------------------------------------------------------------------------- Stream / key
ROUTER_STREAM = "asst:stream:router"
ROUTER_GROUP = "router"
ROUTER_CONSUMER = "router-1"
ROUTER_STREAM_MAXLEN = 10000

WORKER_STREAM_MAXLEN = 200

ROUTER_HEARTBEAT_KEY = "asst:router:heartbeat"
ROUTER_SPAWNING_KEY = "asst:router:spawning"
ROUTER_STATE_KEY = "asst:router:state"

DEDUPE_FEISHU_PREFIX = "asst:dedupe:feishu:"
REPLY_IDEM_PREFIX = "asst:reply:idem:"
SESSION_LOCK_PREFIX = "asst:session:lock:"
QUESTION_ANSWERED_PREFIX = "asst:q:"
TASK_STOP_PREFIX = "asst:task:stop:"
TASK_INFLIGHT_PREFIX = "asst:task:"
TASK_RETRY_PREFIX = "asst:retry:"


def worker_stream(task_id: str) -> str:
    """router → 某 worker 的指令通道（每任务独立，文档 §5.6.1）。"""
    return f"asst:stream:worker:{task_id}"


def worker_group(task_id: str) -> str:
    return f"worker-{task_id}"


def worker_consumer(task_id: str) -> str:
    return f"w-{task_id}-1"


def stop_key(task_id: str) -> str:
    """值 = 当前终止级别 1|2|3（文档 §5.5）。"""
    return f"{TASK_STOP_PREFIX}{task_id}"


def inflight_key(task_id: str) -> str:
    return f"{TASK_INFLIGHT_PREFIX}{task_id}:inflight"


def retry_key(task_id: str) -> str:
    return f"{TASK_RETRY_PREFIX}{task_id}"


def session_lock_key(session_id: str) -> str:
    return f"{SESSION_LOCK_PREFIX}{session_id}"


def question_answered_key(question_id: str) -> str:
    return f"{QUESTION_ANSWERED_PREFIX}{question_id}:answered"


# ----------------------------------------------------------------------------- 信封读写
REDIS_ENTRY_FIELD = "data"


def new_event(
    type: str,
    source: str,
    payload: dict[str, Any],
    *,
    trace_id: str | None = None,
    dedupe_key: str | None = None,
    event_id: str | None = None,
) -> Event:
    """构造一个合法信封（自动补 id / ts / trace_id）。"""
    return Event(
        type=type,
        source=source,
        payload=payload,
        trace_id=trace_id or "",
        dedupe_key=dedupe_key,
        id=event_id or "",
    )


def event_to_redis(e: Event) -> dict[str, str]:
    """XADD 的 fields：单字段 `data` 存整段 JSON，避免字段名漂移。"""
    return {REDIS_ENTRY_FIELD: e.to_json()}


def event_from_redis(fields: dict[str, str]) -> Event:
    """XREADGROUP / XAUTOCLAIM 返回的 fields → Event。"""
    raw = fields.get(REDIS_ENTRY_FIELD)
    if not raw:
        raise ValueError("stream entry 缺少 data 字段")
    return Event.from_json(raw)


# ----------------------------------------------------------------------------- batch 摘要
_MAX_TEXT_IN_DIGEST = 1200


def _clip(text: str, limit: int = _MAX_TEXT_IN_DIGEST) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + f"…（已截断，共 {len(text)} 字）"


def _hhmmss(ts: str) -> str:
    try:
        return ts[11:19]
    except Exception:
        return ts


def render_digest(batch: Iterable[Event], *, now: str | None = None) -> str:
    """把一批事件渲染成本轮唯一的一条 HumanMessage（文档 §4.5）。

    顺序约定：user_message 优先，其余按到达顺序；worker_result 合并为结果清单。
    """
    events = list(batch)
    users = [e for e in events if e.type == "user_message"]
    workers = [e for e in events if e.type in ("worker_result", "worker_message", "worker_notice")]
    timers = [e for e in events if e.type == "timer"]
    systems = [e for e in events if e.type == "system"]

    lines = [f"[唤醒事件 {now or now_iso()}]"]
    for e in users:
        p = e.payload
        lines.append(f"- 用户({_hhmmss(e.ts)})：{_clip(p.get('text', ''))}")
    for e in workers:
        task_id = str(e.payload.get("task_id", ""))[-8:]
        if e.type == "worker_result":
            lines.append(f"- worker({_hhmmss(e.ts)}) 任务 {task_id} 完成，结果：{_clip(e.payload.get('text', '') or '（空）')}")
        elif e.type == "worker_message":
            lines.append(
                f"- worker({_hhmmss(e.ts)}) 任务 {task_id} 提问：{_clip(e.payload.get('text', ''))}"
                f"（deadline {e.payload.get('deadline_s', 30)}s）"
            )
        else:
            lines.append(
                f"- worker({_hhmmss(e.ts)}) 任务 {task_id} 通知({e.payload.get('kind', 'progress')})："
                f"{_clip(e.payload.get('detail', ''))}"
            )
    for e in timers:
        p = e.payload
        lines.append(
            f"- 定时({_hhmmss(e.ts)}) schedule={p.get('schedule_id', '')} 到期：{_clip(p.get('prompt', ''))}"
        )
    for e in systems:
        p = e.payload
        if p.get("kind") == "task_watch":
            lines.append(
                f"- 系统({_hhmmss(e.ts)}) 任务 {str(p.get('task_id', ''))[-8:]} 异常："
                f"{p.get('reason', '')}（不要自动重派，提醒用户三选一）"
            )
        else:
            lines.append(f"- 系统({_hhmmss(e.ts)})：{_clip(str(p))}")

    lines.append("请判断是否需要回复用户；只有调用 ReplyUser 才会真正发消息。")
    return "\n".join(lines)
