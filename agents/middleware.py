"""Agent middleware（契约 §8.3，顺序固定）。

v1 目前只落地了第 3 项 `VisionAdapterMiddleware`：工具（如 `Read`）返回
`image_url` 内容块，而文本模型收到图片块会 400，必须由装配层按模型能力降级，
工具层不判断模型能力（契约 §14-18）。

其余 middleware（Trace / ContextBudget / ToolPolicy / ShellGuard / StopInjection /
WorkerInbox / Cost / ErrorNormalize）尚未实现，属于批次 B/C；实现时必须严格按
§8.3 的顺序插入，不要改变已有序号。
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware

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
