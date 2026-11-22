"""记忆账本：`compact` / `consolidated` 记账与追加式回读（设计 §3.2 / §3.4 / §3.5、D8）。

约定（与 `memory/jsonl.py` 一致，只追加不改写）：

- 记账记录写进 `turns-*.jsonl`，与对话流水共用同一个单调 `seq` 空间；
- `compact`：上下文折叠/压缩发生时追加，写明覆盖的 seq 区间与折叠边界
  （被动安全网；夜间时间戳折叠不走这里，见 MEMORY_DESIGN §8.2）；
- `consolidated`：记忆整理 worker 完成后由 **router** 回写（worker 不直接写流水，
  避免两个进程同时追加导致 `seq` 撞号）；按天记账，抄 worker 的 v2 标记
  （consolidated_day / threads_file / pages_touched），不再有 seq 游标与 summary 版本；
- 读取方遇到 `compact` 跳过 `covers_seq` 区间；
  `query_records()` 默认不返回这两类元记录，需要时传 `include_meta=True`。

本模块只依赖标准库与 `memory/jsonl.py`。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from contracts import TZ_SHANGHAI, now_iso
from memory.jsonl import JsonlWriter

META_TYPES: frozenset[str] = frozenset({"compact", "consolidated"})

_STATE_FILE = "state.json"
_CONSOLIDATION_REPORT = "last-consolidation.json"


# ---------------------------------------------------------------- 内部工具
def _prefix_files(directory: Path, prefix: str) -> list[Path]:
    return sorted(Path(directory).glob(f"{prefix}-*.jsonl"))


def iter_records(directory: Path, *, prefix: str = "turns") -> Iterator[dict]:
    """按文件日期、文件内顺序读取某个前缀的全部记录；坏行跳过。

    `memory/jsonl.read_records` 读目录下**所有** `*.jsonl` 且不吞坏行，这里
    按前缀过滤并容忍崩溃留下的半行，供记账与回读共用。
    """
    for path in _prefix_files(directory, prefix):
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    yield record


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=TZ_SHANGHAI)


def _in_time_range(record: dict, since_ts: str, until_ts: str) -> bool:
    if not since_ts and not until_ts:
        return True
    ts = _parse_ts(record.get("ts"))
    if ts is None:
        return False
    low = _parse_ts(since_ts) if since_ts else None
    high = _parse_ts(until_ts) if until_ts else None
    if low is not None and ts < low:
        return False
    if high is not None and ts > high:
        return False
    return True


def _search_text(record: dict) -> str:
    parts: list[str] = []
    for key in ("digest", "reply", "reason", "task_id", "chat_id", "trace_id", "text", "consolidated_day", "threads_file"):
        value = record.get(key)
        if isinstance(value, str):
            parts.append(value)
    calls = record.get("tool_calls")
    if isinstance(calls, list):
        parts.extend(str(call) for call in calls)
    payload = record.get("payload")
    if isinstance(payload, dict):
        parts.append(json.dumps(payload, ensure_ascii=False))
    return "\n".join(parts)


# ---------------------------------------------------------------- 状态
def load_state(directory: Path) -> dict[str, Any]:
    """读取 `memory/state.json`；不存在/损坏时返回空字典。"""
    path = Path(directory) / _STATE_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(directory: Path, updates: dict[str, Any]) -> dict[str, Any]:
    """原子更新 `memory/state.json`（临时文件 + `os.replace`）。"""
    path = Path(directory) / _STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    state = load_state(directory)
    state.update(updates)
    state["updated_at"] = now_iso()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return state


def consolidation_report_path(directory: Path) -> Path:
    """记忆整理 worker 写、router 读的完成报告（`memory/last-consolidation.json`）。"""
    return Path(directory) / _CONSOLIDATION_REPORT


# ---------------------------------------------------------------- 查询
def turn_records(directory: Path, *, since_seq: int = 0, until_seq: int = 0) -> Iterator[dict]:
    """只产出对话流水（`type` 不在 `META_TYPES` 里的记录）。"""
    for record in iter_records(directory):
        if record.get("type") in META_TYPES:
            continue
        seq = record.get("seq")
        if not isinstance(seq, int):
            continue
        if since_seq and seq < since_seq:
            continue
        if until_seq and seq > until_seq:
            continue
        yield record


def count_turns_after(directory: Path, seq: int) -> int:
    """统计 `seq` 之后还有多少条对话流水（用于判断"该整理了吗"）。"""
    return sum(1 for _ in turn_records(directory, since_seq=int(seq) + 1))


def last_turn_seq(directory: Path) -> int:
    """最后一条对话流水的 seq；空目录返回 0。"""
    last = 0
    for record in turn_records(directory):
        last = max(last, int(record["seq"]))
    return last


def last_compact_cursor(directory: Path) -> int:
    """上一条 `compact` 覆盖到的 seq；没有则 0。"""
    cursor = 0
    for record in iter_records(directory):
        if record.get("type") != "compact":
            continue
        covers = record.get("covers_seq")
        if isinstance(covers, list) and len(covers) == 2 and isinstance(covers[1], int):
            cursor = max(cursor, covers[1])
    return cursor


def query_records(
    directory: Path,
    *,
    prefix: str = "turns",
    since_seq: int = 0,
    until_seq: int = 0,
    since_ts: str = "",
    until_ts: str = "",
    keyword: str = "",
    limit: int = 50,
    include_meta: bool = False,
) -> list[dict]:
    """按 seq / 时间区间 / 关键词回读落盘记录。

    - `prefix`：`turns`（对话流水 + 记账）或 `inbox`（原始事件）；
    - `since_seq` / `until_seq`：闭区间，0 表示不限；
    - `keyword`：大小写不敏感的子串匹配（正文 / 工具名 / payload）；
    - 默认跳过 `compact` / `consolidated` 元记录。
    """
    needle = keyword.strip().lower()
    out: list[dict] = []
    for record in iter_records(directory, prefix=prefix):
        if not include_meta and record.get("type") in META_TYPES:
            continue
        seq = record.get("seq")
        if isinstance(seq, int):
            if since_seq and seq < since_seq:
                continue
            if until_seq and seq > until_seq:
                continue
        if not _in_time_range(record, since_ts, until_ts):
            continue
        if needle and needle not in _search_text(record).lower():
            continue
        out.append(record)
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- 记账
def append_compact(
    directory: Path,
    *,
    reason: str,
    boundary_checkpoint_id: str = "",
    removed_messages: int = 0,
    tokens_before: int = 0,
    tokens_after: int = 0,
    upto_seq: int = 0,
) -> int:
    """追加一条 `compact` 记账，返回它的 seq。

    `covers_seq` 从"上一条 compact 覆盖到的下一条"到 `upto_seq`（折叠发生前
    已落盘的最后一条记录）；区间为空时不写空区间，只记录折叠事件本身。
    """
    cursor = last_compact_cursor(directory)
    end = int(upto_seq)
    covers = [cursor + 1, end] if end >= cursor + 1 else []
    record = {
        "type": "compact",
        "covers_seq": covers,
        "reason": reason,
        "boundary_checkpoint_id": boundary_checkpoint_id,
        "removed_messages": int(removed_messages),
        "tokens_before": int(tokens_before),
        "tokens_after": int(tokens_after),
    }
    return JsonlWriter(Path(directory), "turns").append(record)


def append_consolidated(
    directory: Path,
    *,
    task_id: str,
    consolidated_day: str = "",
    threads_file: str = "",
    pages_touched: list[str] | None = None,
) -> int:
    """追加一条 `consolidated` 记账（记忆整理完成后由 router 回写）。

    MEMORY_DESIGN §8.2：整理以**天**为单位（不再是 seq 游标 / summary 版本）；
    字段直接抄 worker 写的 v2 `last-consolidation.json` 标记，供控制台与对账展示。
    """
    record = {
        "type": "consolidated",
        "task_id": task_id,
        "consolidated_day": str(consolidated_day or ""),
        "threads_file": str(threads_file or ""),
        "pages_touched": [str(p) for p in (pages_touched or [])],
    }
    return JsonlWriter(Path(directory), "turns").append(record)
