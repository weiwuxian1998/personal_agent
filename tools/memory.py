"""记忆整理与回读工具（设计 §3.4 / §3.5、D8）。

两类装配，职责严格分开：

- `build_memory_worker_tools(settings)`：只装配到 `task_kind="memory_consolidate"` 的
  worker 里。它直接读写 `memory/` 目录（通用 fs 工具被 `workspace_root` 限制，够不到这里），
  产出滚动摘要 / 事实 / 决策，并写一份完成报告给 router 回写 `consolidated` 记账；
- `build_memory_router_tools(settings, session_service)`：router 侧入口——
  `ReadRecords`（按 seq / 时间 / 关键词回读 JSONL）、`ReadMemory`（读现有记忆）、
  `ConsolidateMemory`（判断"该整理了"再派整理 worker）。router 只读记忆，不写。

不做定时整理：触发点是 `ConsolidateMemory` 工具，由 router 自己判断后调用。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

from agents.context import current_task_id
from contracts import now_iso, ulid
from memory import ledger
from tools.shell import needs_artifact, stub_text

# 单次回读/整理的硬上限，避免一次把整个流水塞进上下文
MAX_TURNS_PER_READ = 200
MAX_RECORDS_PER_QUERY = 100
_MAX_SUMMARY_CHARS = 8_000
_MAX_FACT_ROWS = 50
_MAX_DECISION_ROWS = 50


# ---------------------------------------------------------------- 记忆文件读写
def _clip(text: Any, limit: int) -> str:
    value = str(text or "")
    return value if len(value) <= limit else value[:limit] + f"…（已截断，共 {len(value)} 字）"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def _append_jsonl(path: Path, record: dict) -> None:
    """记忆文件同样只追加：单行 JSON + flush + fsync。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
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
                rows.append(record)
    return rows


def _memory_snapshot(memory_dir: Path) -> dict:
    """现有记忆的快照（摘要全文 + 最近事实/决策 + 版本状态）。"""
    summary_path = memory_dir / "summary.md"
    try:
        summary = summary_path.read_text(encoding="utf-8")
    except OSError:
        summary = ""
    facts = _read_jsonl(memory_dir / "facts.jsonl")[-_MAX_FACT_ROWS:]
    decisions = _read_jsonl(memory_dir / "decisions.jsonl")[-_MAX_DECISION_ROWS:]
    return {
        "summary": _clip(summary, _MAX_SUMMARY_CHARS),
        "facts": facts,
        "decisions": decisions,
        "state": ledger.load_state(memory_dir),
    }


# ---------------------------------------------------------------- worker 侧
def build_memory_worker_tools(settings: Any) -> list[Any]:
    """记忆整理 worker 的工具集：读流水 → 读旧记忆 → 写新记忆。"""
    memory_dir = Path(settings.memory_dir)

    @tool
    def ReadTurns(since_seq: int = 0, until_seq: int = 0, limit: int = 80) -> dict:
        """读取 router 落盘的对话流水（turns JSONL）。

        since_seq=0 表示从"上次整理之后"自动续读；until_seq=0 表示不限；
        返回每条记录的 seq / ts / digest / reply / tool_calls。
        """
        start = int(since_seq) or (ledger.last_consolidated_seq(memory_dir) + 1)
        cap = max(1, min(int(limit), MAX_TURNS_PER_READ))
        records = list(
            ledger.turn_records(memory_dir, since_seq=start, until_seq=int(until_seq))
        )[:cap]
        items = [
            {
                "seq": record.get("seq"),
                "ts": record.get("ts"),
                "digest": _clip(record.get("digest"), 1500),
                "reply": _clip(record.get("reply"), 1500),
                "tool_calls": record.get("tool_calls") or [],
            }
            for record in records
        ]
        return {
            "ok": True,
            "count": len(items),
            "since_seq": start,
            "records": items,
            "last_seq": ledger.last_turn_seq(memory_dir),
            "note": "只含对话流水；compact/consolidated 记账已跳过。",
        }

    @tool
    def ReadMemory() -> dict:
        """读取现有记忆：滚动摘要、事实表、决策记录与版本状态。"""
        snapshot = _memory_snapshot(memory_dir)
        return {"ok": True, **snapshot}

    @tool
    def WriteMemory(
        summary: str = "",
        facts: list[dict] | None = None,
        decisions: list[dict] | None = None,
        covers_seq: list[int] | None = None,
    ) -> dict:
        """把整理结果写入记忆目录。

        - summary：新的滚动摘要（覆盖旧摘要并 +1 版本；传空串表示本次不改摘要）；
        - facts：事实列表，元素形如 {"key","value","confidence","source_turn"}，
          同 key 的新事实自动 `supersedes` 旧事实（只追加）；
        - decisions：决策列表，元素形如 {"decision","reason","supersedes"}，只追加；
        - covers_seq：[起始 seq, 结束 seq]，声明本次整理消费了哪段流水。
        """
        covers: list[int] = []
        if isinstance(covers_seq, list) and len(covers_seq) == 2:
            try:
                covers = [int(covers_seq[0]), int(covers_seq[1])]
            except (TypeError, ValueError):
                covers = []

        state = ledger.load_state(memory_dir)
        version = int(state.get("summary_version") or 0)
        if summary and summary.strip():
            version += 1
            _atomic_write(memory_dir / "summary.md", summary.strip() + "\n")

        facts_written = 0
        last_fact_ids: dict[str, str] = {}
        for row in _read_jsonl(memory_dir / "facts.jsonl"):
            key = str(row.get("key") or "")
            if key:
                last_fact_ids[key] = str(row.get("id") or "")
        for item in facts or []:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "").strip()
            if not key:
                continue
            record = {
                "id": ulid(),
                "key": key,
                "value": item.get("value"),
                "confidence": item.get("confidence") or "medium",
                "source_turn": item.get("source_turn"),
                "updated_at": now_iso(),
                "supersedes": last_fact_ids.get(key, ""),
            }
            last_fact_ids[key] = record["id"]
            _append_jsonl(memory_dir / "facts.jsonl", record)
            facts_written += 1

        decisions_written = 0
        for item in decisions or []:
            if not isinstance(item, dict):
                continue
            decision = str(item.get("decision") or "").strip()
            if not decision:
                continue
            _append_jsonl(
                memory_dir / "decisions.jsonl",
                {
                    "id": ulid(),
                    "decision": decision,
                    "reason": str(item.get("reason") or ""),
                    "date": now_iso(),
                    "supersedes": str(item.get("supersedes") or ""),
                },
            )
            decisions_written += 1

        updates: dict[str, Any] = {
            "summary_version": version,
            "last_task_id": current_task_id.get() or "",
            "facts_total": len(_read_jsonl(memory_dir / "facts.jsonl")),
            "decisions_total": len(_read_jsonl(memory_dir / "decisions.jsonl")),
        }
        if covers:
            updates["last_consolidated_seq"] = covers[1]
            updates["last_consolidated_ts"] = now_iso()
        ledger.save_state(memory_dir, updates)

        report = {
            "task_id": current_task_id.get() or "",
            "covers_seq": covers,
            "summary_version": version,
            "facts_written": facts_written,
            "decisions_written": decisions_written,
            "ts": now_iso(),
        }
        _atomic_write(
            ledger.consolidation_report_path(memory_dir),
            json.dumps(report, ensure_ascii=False, indent=2),
        )
        return {
            "ok": True,
            "summary_version": version,
            "facts_written": facts_written,
            "decisions_written": decisions_written,
            "covers_seq": covers,
            "note": "记忆已落盘；router 收到 worker_result 后会回写 consolidated 记账。",
        }

    return [ReadTurns, ReadMemory, WriteMemory]


# ---------------------------------------------------------------- router 侧
def _consolidation_prompt(pending: int, start_seq: int, last_seq: int) -> str:
    return f"""你是记忆整理 worker。任务：把 router 的 JSONL 对话流水整理成结构化记忆。

当前待整理区间：seq {start_seq}–{last_seq}（共 {pending} 轮）。
注意：本次任务**没有** Shell / Read / Write，只有下面三个工具：
- ReadTurns：读对话流水（默认接着上次整理的位置）；
- ReadMemory：读现有 summary.md / facts.jsonl / decisions.jsonl；
- WriteMemory：写入新摘要、事实、决策。

步骤：
1. 调 ReadMemory 看现有摘要与事实，避免重复。
2. 调 ReadTurns 读待整理流水（可分批，limit 每次 ≤200）。
3. 产出：
   - summary：滚动摘要，覆盖旧摘要 + 本次流水；一段话总述 + 分主题要点，≤1200 字；
     不写流水账，保留人名、文件路径、关键结论、未完成事项。
   - facts：只记稳定可复用的事实（偏好、路径、账号归属、长期约定），
     每条 {{"key","value","confidence","source_turn"}}，key 用短横线小写英文；
   - decisions：本次区间内定过的决定，每条 {{"decision","reason"}}；
     没有新决策就传空列表。
4. 调 WriteMemory，传 summary / facts / decisions，
   covers_seq=[{start_seq}, {last_seq}]。
5. 最后用两句话说明：整理了多少轮、摘要升到第几版。

不要编造流水里没有的内容；不确定的写成事实时 confidence 用 low。
"""


def build_memory_router_tools(settings: Any, session_service: Any) -> list[Any]:
    """router 侧：回读落盘记录 + 读记忆 + 触发整理。"""
    memory_dir = Path(settings.memory_dir)

    @tool
    def ReadRecords(
        query: str = "",
        source: str = "turns",
        since_seq: int = 0,
        until_seq: int = 0,
        since_ts: str = "",
        until_ts: str = "",
        limit: int = 50,
    ) -> dict:
        """回读落盘的原始记录，不要凭记忆猜测过去发生过什么。

        source="turns"（对话流水，默认）或 "inbox"（原始事件）；
        query 为关键词（大小写不敏感，匹配正文/工具名/事件内容）；
        since_ts/until_ts 形如 "2026-11-08" 或 "2026-11-08T21:30:00+08:00"。
        """
        prefix = "inbox" if str(source).strip().lower() == "inbox" else "turns"
        cap = max(1, min(int(limit), MAX_RECORDS_PER_QUERY))
        records = ledger.query_records(
            memory_dir,
            prefix=prefix,
            since_seq=int(since_seq),
            until_seq=int(until_seq),
            since_ts=str(since_ts or ""),
            until_ts=str(until_ts or ""),
            keyword=str(query or ""),
            limit=cap,
            include_meta=False,
        )
        header = (
            f"ReadRecords source={prefix} 命中 {len(records)} 条"
            f"（since_seq={since_seq} until_seq={until_seq} query={query!r}）"
        )
        text = json.dumps(records, ensure_ascii=False, indent=1)
        if needs_artifact(
            text,
            max_lines=int(settings.tool_output_max_lines),
            max_bytes=int(settings.tool_output_max_bytes),
        ):
            outputs = Path(settings.tool_outputs_dir)
            outputs.mkdir(parents=True, exist_ok=True)
            artifact_path = (outputs / f"records_{ulid()}.json").resolve()
            artifact_path.write_text(text, encoding="utf-8", newline="\n")
            return {
                "ok": True,
                "count": len(records),
                "records": stub_text(text, str(artifact_path), header=header),
            }
        return {"ok": True, "count": len(records), "records": records}

    @tool
    def ReadMemory() -> dict:
        """读取记忆：滚动摘要、事实表、决策记录与版本状态。"""
        return {"ok": True, **_memory_snapshot(memory_dir)}

    @tool
    def ConsolidateMemory() -> dict:
        """派发记忆整理 worker：把尚未整理的对话流水压成摘要/事实/决策。

        没有新流水时返回 no_pending；已有整理任务在跑时返回 already_running。
        派发后本轮就结束，结果会以 worker_result 事件回来。
        """
        last_consolidated = ledger.last_consolidated_seq(memory_dir)
        last_seq = ledger.last_turn_seq(memory_dir)
        pending = ledger.count_turns_after(memory_dir, last_consolidated)
        if pending <= 0:
            return {
                "ok": True,
                "status": "no_pending",
                "last_consolidated_seq": last_consolidated,
                "note": "没有新的对话流水需要整理。",
            }
        running = [
            session
            for session in session_service.list_sessions(status="running", limit=50)
            if str(session.get("title") or "").startswith("记忆整理")
        ]
        if running:
            return {
                "ok": True,
                "status": "already_running",
                "session_id": running[0].get("session_id"),
                "note": "已有整理任务在跑，等它的 worker_result，不要重复派发。",
            }
        result = session_service.create_session(
            _consolidation_prompt(pending, last_consolidated + 1, last_seq),
            title=f"记忆整理 {last_consolidated + 1}-{last_seq}",
            task_kind="memory_consolidate",
        )
        if not result.get("ok"):
            return result
        return {
            **result,
            "pending_turns": pending,
            "covers_seq": [last_consolidated + 1, last_seq],
            "note": "已派发记忆整理 worker；不要等待，结果会以 worker_result 回来。",
        }

    return [ReadRecords, ReadMemory, ConsolidateMemory]
