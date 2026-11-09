"""Agent 装配公共件（M0 主线）。"""

from __future__ import annotations

from typing import Any, Sequence

from langchain.agents import create_agent
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI

from config import Settings


def message_text(message: Any) -> str:
    """把一条消息的内容抽成纯文本。

    内容块可能是 `str` 或 `list[str | dict]`（多模态 / 工具块）；这里只拼接
    `text` 类块，其它块（图片等）忽略，避免把 base64 塞进回复。
    """
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") in ("text", "output_text"):
                parts.append(str(block.get("text", "")))
        return "\n".join(part for part in parts if part).strip()
    return str(content).strip()


def last_ai_text(messages: Sequence[Any]) -> str:
    """取最后一条 AI 消息的文本；没有 AI 消息时返回空串。"""
    for message in reversed(list(messages)):
        if isinstance(message, AIMessage) or getattr(message, "type", "") == "ai":
            return message_text(message)
    return ""


def build_chat_model(settings: Settings) -> ChatOpenAI:
    kwargs: dict[str, Any] = {
        "model": settings.llm_model,
        "base_url": settings.llm_base_url or None,
        "api_key": settings.llm_api_key or "not-set",
        "temperature": 0,
    }
    if settings.llm_thinking_disabled:
        # 契约 §14-38 已实测：`thinking.type=disabled` 在当前端点**确实生效**
        # （reasoning_tokens 0 vs 默认 108–119）。默认关闭是为了成本确定性：
        # 同一问题关闭时 0 reasoning tokens，开启时每轮多 60–120 个输出 token。
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    return ChatOpenAI(**kwargs)


def build_agent(
    *,
    settings: Settings,
    tools: Sequence[Any],
    system_prompt: str,
    checkpointer: Any = None,
    middleware: Sequence[Any] = (),
    name: str = "agent",
):
    return create_agent(
        model=build_chat_model(settings),
        tools=list(tools),
        system_prompt=system_prompt,
        middleware=list(middleware),
        checkpointer=checkpointer,
        name=name,
    )
