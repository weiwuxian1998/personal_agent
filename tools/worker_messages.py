"""`SendMessage`：worker → router 的唯一出口（设计文档 §5.6.4）。

约束：

- **异步**：只 XADD 到 router stream 就返回，不等 router 处理，不阻塞 worker；
- **不直连用户**：用户出口只有 router 的 `ReplyUser`；
- `kind="ask"` 写 `worker_message`（router 需要决策），`kind="notify"` 写
  `worker_notice`（普通进度）；正文就是 `text` 原文，不要求 worker 先拼 JSON。
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import tool

from bus.envelope import ROUTER_STREAM, ROUTER_STREAM_MAXLEN, new_event
from bus.stream import Bus
from contracts import TaskSpec


def build_send_message_tool(*, redis_client: Any, task: TaskSpec):
    bus = Bus(redis_client)

    @tool
    def SendMessage(kind: str, text: str) -> dict:
        """向 router 发一条消息：kind=ask 表示需要 router 决策，notify 表示进度通知。

        调用后立即返回，不等待 router 处理；不要用它给用户回话。

        Args:
            kind: "ask"（需要 router 决策）或 "notify"（普通进度）。
            text: 消息正文，直接写清楚即可，不需要 JSON。
        """
        kind_norm = "ask" if str(kind or "").strip().lower() == "ask" else "notify"
        body = str(text or "").strip()
        if not body:
            return {"ok": False, "error_class": "empty_message", "message": "text 不能为空"}

        if kind_norm == "ask":
            event = new_event(
                "worker_message",
                "worker",
                {
                    "task_id": task.task_id,
                    "text": body,
                    "deadline_s": 30,
                    "chat_id": task.chat_id,
                },
                trace_id=task.trace_id,
            )
        else:
            event = new_event(
                "worker_notice",
                "worker",
                {
                    "task_id": task.task_id,
                    "kind": "progress",
                    "detail": body,
                    "chat_id": task.chat_id,
                },
                trace_id=task.trace_id,
            )
        bus.xadd(event, stream=ROUTER_STREAM, maxlen=ROUTER_STREAM_MAXLEN)
        return {
            "ok": True,
            "queued": True,
            "kind": kind_norm,
            "note": "已投递到 router 输入流，异步处理；继续当前工作即可。",
        }

    return SendMessage
