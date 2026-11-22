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

# 飞书 message_id 形如 `om_x100b60cd0757b4b4cfc715e8be94c01`：om_ 前缀 + 字母数字。
# 这里只做形态判别，不校验该 id 在飞书是否真实存在。
_MAX_MESSAGE_ID_LEN = 80


def _looks_like_message_id(value: str) -> bool:
    return (
        value.isascii()
        and 0 < len(value) <= _MAX_MESSAGE_ID_LEN
        and value.startswith("om_")
        and value.replace("_", "").isalnum()
    )


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
            reply_to: **只接受飞书 message_id**（形如 `om_xxxxxxxx` 的字符串），
                **不要传正文、摘要或整段回答文本**——回答内容一律放进 `text`。
                省略（默认 `None`）= 自动引用本轮触发的那条用户消息；
                传空字符串 `""` = 明确不引用；传具体 `om_...` id = 引用该条消息。
                传其它内容会返回 `invalid_reply_to` 错误，需要改成 message_id 或留空。
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

        # `reply_to` 是 message_id 而不是正文：模型偶尔会把整段回答塞进来，这里给出
        # 可自我纠正的提示，避免冒泡成 "Error invoking tool ... {kwargs}" 的原始异常。
        if reply_to not in (None, "") and not _looks_like_message_id(reply_to):
            preview = " ".join(reply_to.split())[:40]
            return {
                "ok": False,
                "error_class": "invalid_reply_to",
                "message": (
                    f"reply_to 只接受飞书 message_id（om_ 开头的字符串，如 om_x100b60cd…），"
                    f"你传入的是 {len(reply_to)} 字的其它内容「{preview}…」。"
                    "回答正文请放进 text 参数；不想引用就传空字符串 \"\"；"
                    "要引用本轮用户消息就省略 reply_to（默认自动引用）。"
                ),
                "reply_to": preview,
            }

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
            # 幂等命中。键值有两种：真实 message_id（gateway 已发送成功回填）或哨兵
            # "pending"（仍在 outbox 排队，或发送失败后已被 gateway 删除、这是重放窗口）。
            # 不能把哨兵当 message_id 返回：它会经 _collect_reply_calls 写进 turns.jsonl，
            # 污染记账；空的 message_id + 有 slot，才能让 router 的
            # _resolve_reply_message_ids 正常轮询回填真实 id。
            existing = ""
            try:
                existing = str(redis_client.get(key) or "")
            except Exception:  # noqa: BLE001 - redis 抖动不该让回复失败
                logger.warning("读取幂等键失败 slot=%s，按排队中处理", slot, exc_info=True)
            sent_id = existing if existing and existing != "pending" else ""
            base: dict[str, Any] = {
                "ok": True,           # 语义是「不必重发」，不是「已送达」
                "deduped": True,
                "queued": not sent_id,
                "chat_id": chat_id,
                "slot": slot,
                "reply_to": resolved_reply_to or "",
            }
            if sent_id:
                return {
                    **base,
                    "sent": True,
                    "message_id": sent_id,
                    "note": "同一幂等键的回复此前已发出，本次未重复发送。",
                }
            return {
                **base,
                "message_id": "",
                "note": "同一幂等键的回复已在 outbox 排队（或正在重试），本次未重复入队；"
                "真实 message_id 由 gateway 发送成功后回填。",
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
