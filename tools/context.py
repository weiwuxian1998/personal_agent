"""`new_context` 工具（设计文档 §6.4）。

工具本身不做任何删除：它只被 `ContextBudgetMiddleware.wrap_tool_call` 拦下，
置一个"下一轮折叠"的信号，由 `before_model` 逐条发 `RemoveMessage`。
这样既不会与 ToolNode 回填 `ToolMessage` 冲突，也保证删除发生在模型调用之前。
"""

from __future__ import annotations

from langchain_core.tools import tool


@tool
def new_context(keep_recent: int = 8, keep_recent_tokens: int = 40_000, reason: str = "") -> str:
    """折叠较早的上下文，只保留最近的消息，腾出 token 空间后继续当前任务。

    适用场景：任务很长、工具输出很多，感觉上下文快满时主动调用。
    不会删除最后一条用户消息，也不会把 tool_call 与工具结果拆开。
    折叠后如果需要早前的细节，请重新读取文件或重新执行命令，而不是凭记忆回答。

    Args:
        keep_recent: 保留最近多少条消息（默认 8）。
        keep_recent_tokens: 保留窗口的 token 预算（默认 40000）。
        reason: 为什么需要折叠（写入日志，便于排查）。
    """
    return "new_context 未生效：ContextBudgetMiddleware 未装配。"
