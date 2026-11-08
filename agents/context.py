"""工具/中间件共享的 runtime 上下文（冻结）。

gateway 在每轮开始时设置 trace_id 与 chat_id；worker_runner 设置 task_id。
工具（尤其 ReplyUser）只读这些变量，不自己猜。
"""

from __future__ import annotations

from contextvars import ContextVar

current_trace_id: ContextVar[str] = ContextVar("current_trace_id", default="")
current_chat_id: ContextVar[str] = ContextVar("current_chat_id", default="")
current_chat_type: ContextVar[str] = ContextVar("current_chat_type", default="p2p")
current_task_id: ContextVar[str] = ContextVar("current_task_id", default="")
current_tool_call_id: ContextVar[str] = ContextVar("current_tool_call_id", default="")
