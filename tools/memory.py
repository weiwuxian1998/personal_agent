"""记忆工具（router 侧）：回读记账流水（MEMORY_DESIGN §5）。

只有一个装配入口 `build_memory_router_tools(settings)`：

- `ReadRecords`：按 seq / 时间 / 关键词回读 ledger 区 JSONL（turns / inbox）。

派发记忆整理 worker 不做专属工具：直接用通用 `create_session`（`tools/sessions.py`），
传 `task_kind="memory_consolidate"` 即可。原 `ConsolidateMemory` 已被它取代——
它当年做的只是「算日期 + 拼素材路径 + 套一段派单正文」，而素材路径与产出契约
本来就完整写在 `memory_worker_prompt` 里，属于重复。

记忆整理 worker 不装专属工具（§7 已定：与 general worker 同一套通用 fs + Shell，
工作流程写在 `memory_worker_prompt` 里）。旧的 ReadTurns / ReadMemory / WriteMemory
与 facts/decisions/summary 三件套已废弃：记忆本体是 index.md + threads/ + 自由
wiki 页，router 用通用 Read 读，唯一写方是夜间 worker。

不做定时整理：由 gateway 的 `ScheduleScanner` 直发（`schedules.dispatch="worker"`），
不经过 router；worker 完成后 `worker_result` 照常回到 router 记账。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

from contracts import ulid
from memory import ledger
from memory.layout import ledger_dir
from tools.shell import needs_artifact, stub_text

# 单次回读的硬上限，避免一次把整个流水塞进上下文
MAX_RECORDS_PER_QUERY = 100


# ---------------------------------------------------------------- router 侧
def build_memory_router_tools(settings: Any) -> list[Any]:
    """router 侧：回读落盘记录。记忆本体用通用 Read 读 index.md / threads。"""
    ledger_root = ledger_dir(settings)

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
            ledger_root,
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

    return [ReadRecords]
