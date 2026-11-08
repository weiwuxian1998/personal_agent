"""ReplyUser：router 唯一的用户出口（M-ROUTER 归属，主线先实现最小版）。"""

from __future__ import annotations

from itertools import count
from typing import Any

from langchain_core.tools import tool

from agents.context import current_chat_id, current_trace_id
from bus.envelope import REPLY_IDEM_PREFIX, ROUTER_STATE_KEY
from channel.base import Channel


def build_reply_user_tool(*, channel: Channel, redis_client: Any, ttl_s: int = 86400):
    """构造 ReplyUser 工具；`channel` 是唯一允许调用 `send_text` 的对象。"""
    reply_index = count(1)

    @tool
    def ReplyUser(
        text: str,
        reply_to: str | None = None,
        quote: bool = False,
        idempotency_key: str | None = None,
    ) -> dict:
        """把一条消息发给用户。这是 router 唯一的用户出口。

        Args:
            text: 要发送的正文（纯文本，中文）。
            reply_to: 可选，要回复的飞书 message_id。
            quote: 保留参数；v1 不额外引用原文。
            idempotency_key: 可选；同一轮重复调用时用它去重。
        """
        body = (text or "").strip()
        if not body:
            return {"ok": False, "error_class": "empty_text", "message": "回复正文为空"}

        chat_id = current_chat_id.get()
        if not chat_id:
            # worker_result 唤醒时没有原始用户事件，用最近一次会话兜底。
            chat_id = redis_client.hget(ROUTER_STATE_KEY, "last_chat_id") or ""
        if not chat_id:
            return {
                "ok": False,
                "error_class": "no_chat_id",
                "message": "没有可用的 chat_id；请先收到一条用户消息",
            }

        trace = current_trace_id.get() or "notrace"
        slot = idempotency_key or f"{trace}:{next(reply_index)}"
        key = f"{REPLY_IDEM_PREFIX}{slot}"
        if not redis_client.set(key, "pending", nx=True, ex=ttl_s):
            return {"ok": True, "deduped": True, "message_id": "", "text": body}

        result = channel.send_text(
            chat_id=chat_id,
            text=body,
            reply_to_message_id=reply_to,
        )
        if not result.ok:
            redis_client.delete(key)   # 发送失败可重试，不能把幂等键留成死键
            return {
                "ok": False,
                "error_class": "send_failed",
                "message": result.error or "渠道发送失败",
                "code": result.code,
            }
        redis_client.set(key, result.message_id, xx=True, ex=ttl_s)
        return {"ok": True, "message_id": result.message_id, "chat_id": chat_id}

    return ReplyUser
