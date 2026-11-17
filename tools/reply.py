"""ReplyUser：router 唯一的用户出口（M-ROUTER 归属）。

router **不直接调用渠道 API**：这里只把一条 `router_message` 事件写进
`OUTBOX_STREAM`，由常驻的 gateway 消费后真正调用 `channel.send_text`。
这样 router 进程不需要 `build_channel()`，也就不会 `import lark_oapi`
（实测该导入 47–51s，是旧冷启动的主要耗时）。
"""

from __future__ import annotations

import logging
from itertools import count
from typing import Any

from langchain_core.tools import tool

from agents.context import current_chat_id, current_message_id, current_trace_id
from bus.envelope import (
    OUTBOX_STREAM,
    OUTBOX_STREAM_MAXLEN,
    REPLY_IDEM_PREFIX,
    ROUTER_STATE_KEY,
    new_event,
)

logger = logging.getLogger("reply")


def build_reply_user_tool(*, bus: Any, redis_client: Any, ttl_s: int = 86400):
    """构造 ReplyUser 工具；它只写 outbox，不碰渠道实现。"""
    reply_index = count(1)

    @tool
    def ReplyUser(
        text: str,
        reply_to: str | None = None,
        quote: bool = False,
        idempotency_key: str | None = None,
        products: list[str] | None = None,
    ) -> dict:
        """把一条消息发给用户。这是 router 唯一的用户出口。

        Args:
            text: 要发送的正文（纯文本，中文）。只发文件时可以留空。
            reply_to: 要引用的飞书 message_id。默认 `None` = 自动引用本轮触发消息
                （`current_message_id`）；传空串 `""` = 不引用；传具体 id = 引用该条。
            quote: 保留参数；v1 不额外引用原文（引用行为由 `reply_to` 决定）。
            idempotency_key: 可选；同一轮重复调用时用它去重。
            products: 可选，要发给用户的文件/链接列表（**绝对路径**或 http(s) URL）。
                本地图片走飞书图片消息，其它文件走文件消息，URL 作为文本链接发出；
                多个按顺序发送。文件不存在 / 超过平台上限（图片 10 MiB、其它 25 MiB）/
                类型不支持时，会降级为把路径当文本发出去并说明原因。
        """
        body = (text or "").strip()
        files = [str(item).strip() for item in (products or []) if str(item).strip()]
        if not body and not files:
            return {"ok": False, "error_class": "empty_text", "message": "回复正文与附件都为空"}

        # `None` = 引用本轮触发的那条消息；`""` = 明确不引用（定时任务/worker 回报
        # 等没有触发消息的轮次，contextvar 为空，自然不会硬编 reply_to）。
        resolved_reply_to = current_message_id.get() if reply_to is None else reply_to

        chat_id = current_chat_id.get()
        if not chat_id:
            # 兜底只允许在事件确实没带 chat_id 时生效，并且必须留告警：
            # worker_result / send_failed 现在都会带 chat_id（contracts.TaskSpec.chat_id）。
            chat_id = redis_client.hget(ROUTER_STATE_KEY, "last_chat_id") or ""
            logger.warning(
                "ReplyUser 缺少 current_chat_id，回退到 last_chat_id=%s（事件未带会话归属）",
                chat_id or "<empty>",
            )
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
            # 已发送过就返回真实 message_id；仍在排队则返回 pending。
            return {
                "ok": True,
                "deduped": True,
                "message_id": redis_client.get(key) or "",
                "reply_to": resolved_reply_to or "",
                "text": body,
                "products": files,
            }

        event = new_event(
            "router_message",
            "router",
            {
                "kind": "reply",
                "chat_id": chat_id,
                "text": body,
                "reply_to": resolved_reply_to or "",
                "idem_slot": slot,
                # 附件列表随事件一起落库/落日志，方便事后核对发过什么。
                "products": files,
            },
            trace_id=trace,
        )
        try:
            bus.xadd(event, stream=OUTBOX_STREAM, maxlen=OUTBOX_STREAM_MAXLEN)
        except Exception as exc:  # 入队失败必须删幂等键，否则这条回复会被永久丢弃
            redis_client.delete(key)
            return {"ok": False, "error_class": "enqueue_failed", "message": f"{type(exc).__name__}: {exc}"}

        return {
            "ok": True,
            "queued": True,
            "chat_id": chat_id,
            "slot": slot,
            "reply_to": resolved_reply_to or "",
            "products": files,
        }

    return ReplyUser
