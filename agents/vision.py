"""图片内容块的模型能力适配（装配层）。

工具层（如 `Read`）始终返回 `image_url` 内容块；文本模型直接收到会 400。
本模块只做纯转换，由 `VisionAdapterMiddleware` 在每次模型调用前调用：

- `llm_supports_vision=True`：原样返回；
- `False`：把 image 块换成等量文本占位，其余块（含 `[图片] 路径...` 文本）保留；
- 不改 checkpoint 里的 state，只影响本次请求。
"""

from __future__ import annotations

from typing import Any

DEFAULT_PLACEHOLDER = "[图片已省略：当前模型不支持视觉，原图仍在工作区，可换视觉模型后重读]"

_IMAGE_BLOCK_TYPES = frozenset({"image_url", "image"})


def _is_image_block(block: Any) -> bool:
    return isinstance(block, dict) and str(block.get("type", "")) in _IMAGE_BLOCK_TYPES


def adapt_content(
    content: Any,
    *,
    supports_vision: bool,
    placeholder: str = DEFAULT_PLACEHOLDER,
) -> Any:
    """转换单条消息 content；非列表或支持视觉时原样返回。"""
    if supports_vision or not isinstance(content, list):
        return content
    out: list[Any] = []
    for block in content:
        if _is_image_block(block):
            out.append({"type": "text", "text": placeholder})
        else:
            out.append(block)
    return out


def adapt_messages(
    messages: list[Any],
    *,
    supports_vision: bool,
    placeholder: str = DEFAULT_PLACEHOLDER,
) -> list[Any]:
    """返回新的消息列表（不修改入参）；不支持视觉时逐条降级 image 块。

    兼容 langchain BaseMessage 的 `model_copy(update=...)`；
    对没有该方法的对象保持原样，避免在本层引入框架依赖。
    """
    if supports_vision:
        return messages
    out: list[Any] = []
    for message in messages:
        content = getattr(message, "content", None)
        if not isinstance(content, list):
            out.append(message)
            continue
        adapted = adapt_content(content, supports_vision=False, placeholder=placeholder)
        copier = getattr(message, "model_copy", None)
        out.append(copier(update={"content": adapted}) if callable(copier) else message)
    return out
