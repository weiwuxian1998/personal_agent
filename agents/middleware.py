"""Agent middleware（契约 §8.3，顺序固定）。

v1 目前落地了两项：

- `VisionAdapterMiddleware`（§8.3-3）：工具（如 `Read`）返回 `image_url` 内容块，
  而文本模型收到图片块会 400，必须由装配层按模型能力降级，工具层不判断模型能力（契约 §14-18）。
- `ToolContextMiddleware`（§8.3-10）：工具执行前把 `tool_call["id"]` 写进
  `current_tool_call_id`，保证落盘文件名与 trace 可关联。

其余 middleware（Trace / ContextBudget / ToolPolicy / ShellGuard / StopInjection /
WorkerInbox / Cost / ErrorNormalize）尚未实现，属于批次 B/C；实现时必须严格按
§8.3 的顺序插入，不要改变已有序号。

"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware

from agents.context import current_tool_call_id
from agents.vision import adapt_messages
from config import Settings


class VisionAdapterMiddleware(AgentMiddleware):
    """按 `settings.llm_supports_vision` 把图片块降级为文本占位。

    用 `wrap_model_call` 而不是 `before_model`：只改**本次请求**的 messages，
    不写回 graph state；否则 checkpoint 里会永久留下降级后的消息，
    换成支持视觉的模型后也看不到原图。
    """

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._supports_vision = bool(settings.llm_supports_vision)

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        if self._supports_vision:
            return handler(request)
        messages = adapt_messages(request.messages, supports_vision=False)
        return handler(request.override(messages=messages))


class ToolContextMiddleware(AgentMiddleware):
    """把模型产出的 `tool_call["id"]` 写进 `current_tool_call_id`（契约 §7.7 / §8.3-10）。

    落盘路径是 `tool_outputs/<trace_id>/<tool_call_id>-<n>.md`。没有这一步，
    `tools/web.py` / `tools/shell.py` 拿到的 `current_tool_call_id` 为空，文件名退化为
    随机 ULID，trace 与落盘文件无法关联（`scripts/check_web.py` 里手工 set 过，
    所以单测能过、真实图里却丢关联——必须由装配层统一赋值）。

    用 `wrap_tool_call`：调用前 set、调用后 reset，不污染同一轮里的下一个工具。
    """

    def wrap_tool_call(self, request: Any, handler: Any) -> Any:
        call = getattr(request, "tool_call", None) or {}
        call_id = str(call.get("id") or "") if isinstance(call, dict) else str(getattr(call, "id", "") or "")
        if not call_id:
            return handler(request)
        token = current_tool_call_id.set(call_id)
        try:
            return handler(request)
        finally:
            current_tool_call_id.reset(token)

