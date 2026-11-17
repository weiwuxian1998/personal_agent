"""工具/中间件共享的 runtime 上下文（冻结）。

gateway 在每轮开始时设置 trace_id 与 chat_id；worker_runner 设置 task_id。
工具（尤其 ReplyUser）只读这些变量，不自己猜。
"""

from __future__ import annotations

from contextvars import ContextVar

current_trace_id: ContextVar[str] = ContextVar("current_trace_id", default="")
current_chat_id: ContextVar[str] = ContextVar("current_chat_id", default="")
current_chat_type: ContextVar[str] = ContextVar("current_chat_type", default="p2p")
# 本轮触发消息的飞书 message_id：ReplyUser 的 reply_to 为 None 时默认引用它。
# 定时任务 / worker 回报等没有原始用户消息的轮次为空串（不硬编 reply_to）。
current_message_id: ContextVar[str] = ContextVar("current_message_id", default="")
current_task_id: ContextVar[str] = ContextVar("current_task_id", default="")
current_tool_call_id: ContextVar[str] = ContextVar("current_tool_call_id", default="")
# 最近一次 LLM 原始响应里的思考内容（reasoning_content）。
# langchain-openai 会丢弃该字段（契约 §14-46），只能在 httpx 层捕获后放这里，
# 由 AgentTraceMiddleware 读走并写日志。每次模型调用前由中间件清空。
last_llm_reasoning: ContextVar[str] = ContextVar("last_llm_reasoning", default="")
