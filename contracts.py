"""跨模块共享的数据结构（唯一来源）。

**本文件由主线维护，其他模块不得直接修改**。需要新增字段时，在
`docs/ENGINEERING_CONTRACT.md` 的变更流程里提需求，由主线统一改。

注意：不命名为 `types.py`——仓库根目录会出现在 `sys.path` 首位，`types.py`
会遮蔽标准库 `types`，导致 `dataclasses` / `re` 等模块导入失败。


设计约束：
- 只用标准库（不 import redis / langchain），任何模块都能安全导入；
- 所有跨进程边界（Redis 事件、SQLite 行、HTTP/飞书）都用这里的 dataclass 表达；
- 序列化用 `to_dict()` / `from_dict()`，字段名与文档 §3.3 / §4.4 / §9.2 一致。
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

# 本机固定时区（文档 §9.3）：展示与 JSONL 用 +08:00，调度表存 UTC
TZ_SHANGHAI = timezone(timedelta(hours=8))

# 事件类型注册表（文档 §4.4）。新增 type 必须同时改这里与 router 的处理分支。
EVENT_TYPES: frozenset[str] = frozenset(
    {
        "user_message",
        "worker_result",
        "worker_message",
        "worker_notice",
        "router_message",
        "timer",
        "system",
    }
)

# 固定来源枚举（用于校验信封，不限制 payload）
# `console` 是本地调试渠道（契约 §6.3）；正式渠道只有 `feishu`。
EVENT_SOURCES: frozenset[str] = frozenset(
    {"feishu", "console", "worker", "scheduler", "router", "system"}
)

ToolProfile = Literal["router", "worker"]
SessionStatus = Literal["running", "waiting_router", "idle", "stopped", "error", "done", "unreachable"]
TaskKind = Literal["general", "memory_consolidate"]

_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


# ----------------------------------------------------------------------------- ids / time
def ulid(ts_ms: Optional[int] = None) -> str:
    """生成 26 字符 ULID（时间有序 + 80 bit 随机）。

    不引入第三方依赖；同毫秒内靠随机位保证唯一。
    """
    ts = int(time.time() * 1000) if ts_ms is None else int(ts_ms)
    if not 0 <= ts < 2**48:
        raise ValueError("ULID 时间戳超出 48 bit")
    time_part = ""
    for shift in range(45, -1, -5):
        time_part += _ULID_ALPHABET[(ts >> shift) & 0x1F]
    rand = secrets.randbits(80)
    rand_part = "".join(_ULID_ALPHABET[(rand >> shift) & 0x1F] for shift in range(75, -1, -5))
    return time_part + rand_part


def now_iso() -> str:
    """本地时间（+08:00），毫秒精度；用于 JSONL / SQLite 展示字段。"""
    return datetime.now(TZ_SHANGHAI).isoformat(timespec="milliseconds")


def now_utc_iso() -> str:
    """UTC 时间（Z 结尾），毫秒精度；用于调度表 next_run_utc。"""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    """解析 ISO8601；无时区按 UTC。调度比较必须走这个函数，不要字符串比较。"""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ----------------------------------------------------------------------------- 事件信封
@dataclass(slots=True)
class Event:
    """Redis Stream 的唯一事件信封（文档 §4.4）。

    写入 Redis 时整体序列化为 JSON，放在字段 `data` 里；stream 的 entry id
    由 Redis 生成，与本结构的 `id`（ULID）无关。
    """

    type: str
    source: str
    payload: dict[str, Any]
    v: int = 1
    id: str = ""
    ts: str = ""
    trace_id: str = ""
    dedupe_key: Optional[str] = None

    def __post_init__(self) -> None:
        if self.type not in EVENT_TYPES:
            raise ValueError(f"未注册的事件类型: {self.type}")
        if self.source not in EVENT_SOURCES:
            raise ValueError(f"未注册的事件来源: {self.source}")
        if not self.id:
            self.id = ulid()
        if not self.ts:
            self.ts = now_iso()
        if not self.trace_id:
            self.trace_id = self.id
        if not isinstance(self.payload, dict):
            raise TypeError("payload 必须是 dict")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Event":
        return cls(
            type=data["type"],
            source=data["source"],
            payload=data.get("payload") or {},
            v=int(data.get("v", 1)),
            id=data.get("id") or "",
            ts=data.get("ts") or "",
            trace_id=data.get("trace_id") or "",
            dedupe_key=data.get("dedupe_key"),
        )

    @classmethod
    def from_json(cls, raw: str) -> "Event":
        return cls.from_dict(json.loads(raw))


# ----------------------------------------------------------------------------- 渠道
@dataclass(slots=True)
class IncomingMessage:
    """平台无关的入站消息（channel 层产出，gateway 消费）。"""

    channel: str            # "feishu" / "console"
    chat_id: str
    chat_type: str          # "p2p" / "group"
    message_id: str         # 幂等去重键（飞书官方要求用 message_id）
    event_id: str           # 仅日志
    sender_id: str
    text: str
    create_time: int        # 平台侧毫秒时间戳；0 表示未知
    thread_id: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SendResult:
    """发送结果。渠道实现失败不抛异常，统一用它表达。"""

    ok: bool
    message_id: str = ""
    error: str = ""
    code: Optional[int] = None


# ----------------------------------------------------------------------------- SQLite 行
@dataclass(slots=True)
class SessionRecord:
    """`sessions` 表一行（文档 §9.2）。"""

    id: str
    title: str
    status: SessionStatus
    cwd: Optional[str] = None
    pid: Optional[int] = None
    pid_started_at: Optional[str] = None   # 进程创建时间，ISO8601；用于 PID 复用判定
    parent_id: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""
    first_message_at: Optional[str] = None  # NULL = 派发后尚未收到任何 worker 消息

    @classmethod
    def from_row(cls, row: Any) -> "SessionRecord":
        return cls(
            id=row["id"],
            title=row["title"],
            status=row["status"],
            cwd=row["cwd"],
            pid=row["pid"],
            pid_started_at=row["pid_started_at"],
            parent_id=row["parent_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            first_message_at=row["first_message_at"],
        )


@dataclass(slots=True)
class ScheduleRecord:
    """`schedules` 表一行。`next_run_utc` 必须是 UTC ISO8601。"""

    id: str
    cron: Optional[str]
    at: Optional[str]
    timezone: str
    prompt: str
    status: str
    next_run_utc: Optional[str]
    last_run_utc: Optional[str]
    misfire_grace_s: int
    catch_up: str
    created_at: str

    @classmethod
    def from_row(cls, row: Any) -> "ScheduleRecord":
        return cls(
            id=row["id"],
            cron=row["cron"],
            at=row["at"],
            timezone=row["timezone"],
            prompt=row["prompt"],
            status=row["status"],
            next_run_utc=row["next_run_utc"],
            last_run_utc=row["last_run_utc"],
            misfire_grace_s=row["misfire_grace_s"],
            catch_up=row["catch_up"],
            created_at=row["created_at"],
        )


# ----------------------------------------------------------------------------- worker 任务
@dataclass(slots=True)
class TaskSpec:
    """`workers/<task_id>/task.json`（文档 §2.3 / §5.3）。

    router 的 `create_session` 写入，`worker_runner.py` 读取。
    `cwd` 是运行目录；`workspace_root` 是工具允许写入的根。
    """

    task_id: str
    prompt: str
    title: str
    trace_id: str
    cwd: str
    workspace_root: str
    tools_profile: ToolProfile = "worker"
    task_kind: TaskKind = "general"
    resume: bool = False
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = now_iso()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskSpec":
        return cls(**data)

    @classmethod
    def from_json(cls, raw: str) -> "TaskSpec":
        return cls.from_dict(json.loads(raw))


# ----------------------------------------------------------------------------- 工具返回
def tool_error(code: str, message: str, *, hint: str = "", **extra: Any) -> dict[str, Any]:
    """工具失败的标准返回体（不抛异常）。`code` 是稳定字符串，供模型判断。"""
    out: dict[str, Any] = {"ok": False, "error_class": code, "message": message}
    if hint:
        out["hint"] = hint
    out.update(extra)
    return out
