"""上下文预算与折叠算法（设计文档 §3.6）。

这里只放**纯函数**：token 估算、切点计算、成对性校验。装配在
`agents.middleware.ContextBudgetMiddleware`，工具在 `tools.context.new_context`。

切点规则（与文档一致，按优先级从高到低）：

1. 至少保留最近 `keep_recent` 条消息；
2. 宁可多留，也不能把 `AIMessage(tool_calls)` 与其后的 `ToolMessage` 拆开；
3. 保留最近 `keep_recent_tokens` 个 token 的窗口（超出就从最旧的成对边界往外扩）；
4. **最后一条 `HumanMessage` 必须保留**：窗口里没有它就把切点前移到它的位置。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Sequence

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

# 中文/日文/韩文按 1 token/字，其余按 4 字符/token（宁可高估，不要低估）。
_CJK_RANGES = ((0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0x3040, 0x30FF), (0xAC00, 0xD7AF))


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return any(lo <= code <= hi for lo, hi in _CJK_RANGES)


def estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = sum(1 for ch in text if _is_cjk(ch))
    return cjk + max(0, (len(text) - cjk) // 4)


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(str(block.get("text") or block.get("content") or ""))
        return "\n".join(parts)
    return str(content or "")


def message_tokens(message: BaseMessage) -> int:
    total = estimate_text_tokens(_content_text(getattr(message, "content", "")))
    tool_calls = getattr(message, "tool_calls", None) or []
    if tool_calls:
        try:
            total += estimate_text_tokens(json.dumps(tool_calls, ensure_ascii=False))
        except Exception:  # noqa: BLE001 - tool_calls 正常都可序列化，兜底不阻断折叠
            total += estimate_text_tokens(str(tool_calls))
    return total


def estimate_tokens(messages: Sequence[BaseMessage]) -> int:
    return sum(message_tokens(m) for m in messages)


@dataclass(frozen=True)
class FoldPlan:
    """折叠计划：切点之前（不含切点）的消息将被删除。"""

    cut_index: int
    removed: int
    keep_last_human: int | None
    truncated_tools: int = 0


def _safe_cut(messages: Sequence[BaseMessage], start: int) -> int:
    """把切点向左调整到 tool_call / ToolMessage 成对边界之外。

    只处理「切点落在 ToolMessage 上、其 AIMessage 在切点之前」这一种拆对：
    向左滑到该 AIMessage（整组一起留，宁可多留）。切点落在带 tool_calls 的
    AIMessage 上时无需调整——ToolMessage 必然在它之后，必然保留在窗口内。
    悬空/孤儿（历史里本来就没有对应 ToolMessage）由
    `DanglingToolCallMiddleware` 在模型调用前兜底，这里不做防御。
    """
    n = len(messages)
    start = max(0, min(start, n))
    # messages[start] 是 ToolMessage：它属于前面某条 AIMessage 的 tool_calls，整组一起留。
    while start > 0 and isinstance(messages[start], ToolMessage):
        start -= 1
        # 继续左移直到碰到 AIMessage（其 tool_calls 包含当前消息 id）
        while start > 0 and not isinstance(messages[start], AIMessage):
            start -= 1
    return start


def compute_cut(
    messages: Sequence[BaseMessage],
    *,
    keep_recent: int,
    keep_recent_tokens: int,
) -> int:
    """返回应保留窗口的起始下标；0 表示不需要折叠。"""
    n = len(messages)
    if n <= keep_recent:
        return 0

    start = _safe_cut(messages, n - keep_recent)

    # 3) 从最旧的成对边界开始丢，直到窗口 token 落在预算内。
    while start < n - 1 and estimate_tokens(messages[start:]) > keep_recent_tokens:
        next_start = _safe_cut(messages, start + 1)
        if next_start <= start or next_start >= n:
            break
        start = next_start

    # 4) 最后一条 HumanMessage 必须保留。
    last_human = None
    for i in range(n - 1, -1, -1):
        if isinstance(messages[i], HumanMessage):
            last_human = i
            break
    if last_human is not None and last_human < start:
        start = last_human

    start = _safe_cut(messages, start)
    return start if start > 0 else 0


def truncate_oversized_tool_messages(
    messages: list[BaseMessage],
    *,
    keep_last: int = 4,
    max_chars: int = 4000,
) -> int:
    """硬预算兜底：把窗口内过长 ToolMessage 的正文截断（最近 `keep_last` 条不动）。

    返回截断的条数。原地修改传入列表。
    """
    limit = max(0, len(messages) - max(0, keep_last))
    truncated = 0
    for index in range(limit):
        message = messages[index]
        if not isinstance(message, ToolMessage):
            continue
        text = _content_text(message.content)
        if len(text) <= max_chars:
            continue
        message.content = text[:max_chars] + f"\n…[上下文预算：正文已截断，仅保留前 {max_chars} 字符]"
        truncated += 1
    return truncated


def fold_plan(
    messages: Sequence[BaseMessage],
    *,
    keep_recent: int,
    keep_recent_tokens: int,
    hard_tokens: int,
) -> FoldPlan:
    cut = compute_cut(messages, keep_recent=keep_recent, keep_recent_tokens=keep_recent_tokens)
    last_human = None
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], HumanMessage):
            last_human = i
            break
    plan = FoldPlan(cut_index=cut, removed=cut, keep_last_human=last_human)
    if cut and estimate_tokens(messages[cut:]) > hard_tokens:
        # 不能在纯函数里改消息，返回提示由调用方决定是否截断。
        plan = FoldPlan(cut_index=cut, removed=cut, keep_last_human=last_human, truncated_tools=-1)
    return plan
