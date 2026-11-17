"""Agent 装配公共件（M0 主线）。"""

from __future__ import annotations

import json
import logging
from typing import Any, Sequence

import httpx
from langchain.agents import create_agent
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI

from agents.context import last_llm_reasoning
from config import Settings

# 日志截断上限（字）：防止单条日志被工具大输出撑爆，同时保留头部的关键信息。
_LOG_CLIP_ARGS = 400
_LOG_CLIP_REPLY = 800
_LOG_CLIP_REASONING = 1200
_LOG_CLIP_TOOL_RESULT = 600


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


def _capture_reasoning(response: httpx.Response) -> None:
    """httpx 响应钩子：从 chat/completions 原始 JSON 里抢救 `reasoning_content`。

    langchain-openai 1.7.4 转 AIMessage 时会丢弃该字段（契约 §14-46），要看到
    模型思考只能在这里截获。解析失败不影响请求本身。
    """
    try:
        if "/chat/completions" not in str(response.request.url):
            return
        # SSE 流式响应不缓冲，只有非流式 JSON 才解析；钩子触发时 body 尚未读取，
        # 必须先 read()（openai SDK 随后会再读，read() 幂等，无副作用）。
        if "json" not in response.headers.get("content-type", ""):
            return
        response.read()
        data = response.json()
        parts: list[str] = []
        for choice in data.get("choices") or []:
            message = choice.get("message") or {}
            reasoning = message.get("reasoning_content") or message.get("reasoning")
            if reasoning:
                parts.append(str(reasoning))
        if parts:
            last_llm_reasoning.set("\n".join(parts))
    except Exception:
        pass


def build_chat_model(settings: Settings) -> ChatOpenAI:
    kwargs: dict[str, Any] = {
        "model": settings.llm_model,
        "base_url": settings.llm_base_url or None,
        "api_key": settings.llm_api_key or "not-set",
        "temperature": 0,
    }
    extra_body: dict[str, Any] = {}
    if settings.llm_thinking_disabled:
        # `thinking.type=disabled` 在直连端点实测生效（reasoning_tokens 归零）。
        extra_body["thinking"] = {"type": "disabled"}
    elif settings.llm_reasoning_effort.strip():
        # 思考保持开启，只压低档位；留空表示不发该字段，避免给其他端点塞脏参数。
        extra_body["reasoning_effort"] = settings.llm_reasoning_effort.strip()
    if extra_body:
        kwargs["extra_body"] = extra_body
    if not settings.llm_thinking_disabled:
        # 思考开启时挂 httpx 钩子截获 reasoning_content（模型日志可见，见 middleware.py）。
        # httpx 默认 trust_env=True，环境代理行为不变。
        kwargs["http_client"] = httpx.Client(event_hooks={"response": [_capture_reasoning]})
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


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"…（截断，共 {len(text)} 字）"


def _fmt_args(args: Any) -> str:
    try:
        return json.dumps(args, ensure_ascii=False, default=str)
    except Exception:
        return str(args)


def _log_stream_message(logger: logging.Logger, node: str, message: Any) -> None:
    """把流式产出的单条消息写成人类可读的日志。"""
    lines: list[str] = []
    if getattr(message, "type", "") == "ai":
        # 思考内容由 httpx 钩子截获（langchain-openai 会丢弃，契约 §14-46），读走即清空。
        reasoning = (last_llm_reasoning.get() or "").strip()
        last_llm_reasoning.set("")
        if reasoning:
            lines.append(f"思考：{_clip(reasoning, _LOG_CLIP_REASONING)}")
        for call in getattr(message, "tool_calls", None) or []:
            if isinstance(call, dict):
                name, args = str(call.get("name") or "?"), call.get("args")
            else:
                name, args = str(getattr(call, "name", "?")), getattr(call, "args", None)
            lines.append(f"工具调用 {name} args={_clip(_fmt_args(args), _LOG_CLIP_ARGS)}")
        reply = message_text(message)
        if reply:
            lines.append(f"回复：{_clip(reply, _LOG_CLIP_REPLY)}")
        usage = getattr(message, "usage_metadata", None)
        if usage:
            lines.append(
                "用量：输入 %s / 输出 %s / 总 %s tokens"
                % (usage.get("input_tokens"), usage.get("output_tokens"), usage.get("total_tokens"))
            )
        if lines:
            logger.info("[%s] 模型产出\n%s", node, "\n".join(lines))
    elif getattr(message, "type", "") == "tool":
        name = str(getattr(message, "name", "") or "?")
        status = str(getattr(message, "status", "") or "success")
        logger.info(
            "[%s] 工具完成 %s status=%s 结果：%s",
            node,
            name,
            status,
            _clip(message_text(message), _LOG_CLIP_TOOL_RESULT),
        )


def stream_agent_steps(
    graph: Any,
    inputs: dict[str, Any],
    config: dict[str, Any],
    *,
    logger: logging.Logger,
) -> list[Any]:
    """以 `stream_mode="updates"` 跑 agent，把每一步实时写日志（替代黑盒 invoke）。

    每次模型调用产出一条 AIMessage（思考 / 工具调用及参数 / 回复 / 用量），
    每次工具执行产出一条 ToolMessage（工具名 / 状态 / 结果摘要），逐条落日志。
    返回本轮累计的全部消息，与 `graph.invoke(...).messages` 等价，调用方继续用
    `last_ai_text` / 工具名统计处理。
    """
    messages: list[Any] = []
    for chunk in graph.stream(inputs, config, stream_mode="updates"):
        for node, payload in (chunk or {}).items():
            delta = (payload or {}).get("messages") or []
            for message in delta:
                messages.append(message)
                try:
                    _log_stream_message(logger, node, message)
                except Exception:
                    logger.warning("日志渲染失败（不影响执行）", exc_info=True)
    return messages
