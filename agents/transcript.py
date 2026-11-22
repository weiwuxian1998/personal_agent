"""Router transcript 流水 + 消息 created_at 打标（docs/MEMORY_DESIGN.md §4 / §8.2）。

两件事，一个中间件（仅 router 装配；worker 上下文随任务结束丢弃，不需要）：

1. **transcript（journal-at-production）**：消息进入 router 上下文时就逐条追加
   `DATA_DIR/router/transcript/transcript-YYYY-MM-DD.jsonl`（复用 JsonlWriter：
   按天轮转、seq 递增、fsync）。不能夜里从 checkpoint 导出——白天被动折叠
   （RemoveMessage）之后 checkpoint 里已经没有被删的历史（§4 关键约束）。

2. **created_at 打标（§8.2-1）**：给 state 中缺 `additional_kwargs["created_at"]`
   的消息补 Asia/Shanghai ISO 时间戳，并把处理后的消息作为 state 更新返回
   （add_messages reducer 按同 id 替换，随 checkpoint 持久化）。启动时的
   「时间戳折叠」靠它判断"今天 00:00 之前"。必须走 before_model/after_model
   而不是 wrap_model_call——后者只改请求不写 state，打标不会持久化。
   幂等键是 `additional_kwargs["transcript_journaled"]`（而非 created_at 存在与否），
   这样夜间折叠注入的自带 created_at 的 SystemMessage 也能入流水。

记录点分工（§4）：`before_model` 记新进的 HumanMessage / ToolMessage / 注入块，
`after_model` 记本轮新产出的 AIMessage（router 空闲即退出，最后一轮的 AIMessage
不会再经过 before_model，必须由 after_model 兜住）。

容错：极端崩溃下 state 更新没落 checkpoint，同一条消息可能被打标/记录两次；
消费方（记忆整理 worker）按 msg_id 去重即可，transcript 允许重复行。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from agents.context import current_trace_id
from config import Settings
from contracts import now_iso
from memory.jsonl import JsonlWriter
from memory.layout import transcript_dir

logger = logging.getLogger("agents.transcript")

_CREATED_AT = "created_at"
_JOURNALED = "transcript_journaled"


def _content_for_journal(content: Any) -> Any:
    """正文全量入流水，但图片块只留占位（base64 会把流水撑大几十倍，契约同 §8.3-3 思路）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        blocks: list[Any] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in ("image_url", "image"):
                blocks.append({"type": "image", "note": "[图片块，不入流水]"})
            elif isinstance(block, (dict, str)):
                blocks.append(block)
            else:
                blocks.append(str(block))
        return blocks
    return str(content)


def _tool_calls_for_journal(tool_calls: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for call in tool_calls or []:
        if isinstance(call, dict):
            rows.append(
                {
                    "id": str(call.get("id") or ""),
                    "name": str(call.get("name") or ""),
                    "args": call.get("args"),
                }
            )
        else:
            rows.append(
                {
                    "id": str(getattr(call, "id", "") or ""),
                    "name": str(getattr(call, "name", "") or ""),
                    "args": getattr(call, "args", None),
                }
            )
    return rows


def message_record(message: Any) -> dict[str, Any]:
    """消息 → transcript 记录（`v`/`seq`/`ts` 由 JsonlWriter 补，形状见 §4）。"""
    record: dict[str, Any] = {
        "kind": "message",
        "msg_id": str(getattr(message, "id", "") or ""),
        "role": str(getattr(message, "type", "") or ""),
        "created_at": str(
            (getattr(message, "additional_kwargs", None) or {}).get(_CREATED_AT) or ""
        ),
        "content": _content_for_journal(getattr(message, "content", "")),
    }
    trace_id = current_trace_id.get()
    if trace_id:
        record["trace_id"] = trace_id
    tool_calls = getattr(message, "tool_calls", None) or []
    if tool_calls:
        record["tool_calls"] = _tool_calls_for_journal(tool_calls)
    if isinstance(message, ToolMessage):
        record["name"] = str(getattr(message, "name", "") or "")
        record["tool_call_id"] = str(getattr(message, "tool_call_id", "") or "")
        record["status"] = str(getattr(message, "status", "") or "")
    usage = getattr(message, "usage_metadata", None)
    if usage:
        try:
            record["usage"] = dict(usage)
        except Exception:  # noqa: BLE001 - usage 形状异常不影响记录正文
            record["usage"] = str(usage)
    return record


class TranscriptMiddleware(AgentMiddleware):
    """打标 + 流水（模块 docstring 即设计说明；顺序上应排在其他 middleware 之前）。"""

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._writer = JsonlWriter(transcript_dir(settings), "transcript")

    # ------------------------------------------------------------------ 内部
    def _journal(self, message: Any) -> None:
        record = message_record(message)
        try:
            self._writer.append(record)
        except Exception:  # noqa: BLE001 - 流水写失败不阻塞对话
            try:
                # 兜底：args 里混入不可序列化对象时转字符串重写一次。
                sanitized = json.loads(json.dumps(record, ensure_ascii=False, default=str))
                self._writer.append(sanitized)
            except Exception:
                logger.warning(
                    "transcript 流水写入失败 msg_id=%s（不影响执行）",
                    getattr(message, "id", ""),
                    exc_info=True,
                )

    def _stamp_and_journal(self, messages: list[Any]) -> list[Any]:
        """给缺 created_at 的消息打标、给未入流水的消息记流水，返回被处理的消息。

        幂等键是 `additional_kwargs["transcript_journaled"]` 而不是 created_at 是否存在：
        夜间折叠注入的 SystemMessage 自带 created_at（§8.2），但同样需要入流水。
        """
        stamped: list[Any] = []
        for message in messages:
            kwargs = getattr(message, "additional_kwargs", None)
            if not isinstance(kwargs, dict) or kwargs.get(_JOURNALED):
                continue  # 已处理过（含历史 checkpoint 里的旧消息）→ 幂等跳过
            if not kwargs.get(_CREATED_AT):
                kwargs[_CREATED_AT] = now_iso()
            self._journal(message)
            kwargs[_JOURNALED] = True
            stamped.append(message)
        return stamped

    @staticmethod
    def _as_update(stamped: list[Any]) -> dict[str, Any] | None:
        # 只回传有 id 的消息：add_messages 按 id 替换；无 id 会被当新消息追加造成重复
        #（打标已原地生效，最坏该消息下次进 before_model 再补一次更新）。
        with_id = [m for m in stamped if getattr(m, "id", None)]
        return {"messages": with_id} if with_id else None

    # ------------------------------------------------------------------ 钩子
    def before_model(self, state: Any, runtime: Any = None) -> dict[str, Any] | None:
        messages = list((state or {}).get("messages") or [])
        return self._as_update(self._stamp_and_journal(messages))

    def after_model(self, state: Any, runtime: Any = None) -> dict[str, Any] | None:
        messages = list((state or {}).get("messages") or [])
        # 本轮新产出的 AIMessage 在末尾；旧消息在 before_model 已打过标。
        return self._as_update(self._stamp_and_journal(messages[-1:]))
