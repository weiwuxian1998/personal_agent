"""渠道抽象层（**接口冻结，由主线维护**）。

飞书 / 控制台都实现 `Channel`。gateway 只依赖这三个符号，不 import 具体实现。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Callable

from contracts import TZ_SHANGHAI, IncomingMessage, SendResult


MessageHandler = Callable[[IncomingMessage], None]


def format_query_time(create_time_ms: int) -> str:
    """平台毫秒时间戳 → UTC+8 ISO 8601（带 `+08:00`）；缺失/非法退回当前时刻。"""
    try:
        seconds = int(create_time_ms or 0) / 1000
    except (TypeError, ValueError):
        seconds = 0
    moment = (
        datetime.fromtimestamp(seconds, TZ_SHANGHAI)
        if seconds > 0
        else datetime.now(TZ_SHANGHAI)
    )
    return moment.isoformat(timespec="seconds")


def wrap_user_query(
    text: str,
    create_time_ms: int = 0,
    attachments: list[str] | None = None,
) -> str:
    """固定包装用户输入（在事件进入 router 之前调用）。

    形如：`<user_query>原话</user_query><query_time>2026-11-09T21:30:12+08:00</query_time>`
    后面按顺序追加 `<attachment path="绝对路径"/>`。用户原话不转义、不改写。
    因为包装发生在 gateway 入队处，JSONL 落盘的 `text` 也是包装后的这份。
    """
    parts = [
        f"<user_query>{text or ''}</user_query>",
        f"<query_time>{format_query_time(create_time_ms)}</query_time>",
    ]
    for path in attachments or []:
        if path:
            parts.append(f'<attachment path="{path}"/>')
    return "".join(parts)


class Channel(ABC):
    """所有 IM 渠道的统一接口。

    约定：
    - `send_text` 失败不抛异常，用 `SendResult` 表达；
    - `run_forever` 阻塞当前线程直到进程退出（飞书 SDK 的长连接就是阻塞的）；
    - `on_message` 回调必须在毫秒级返回（只做去重 + XADD），否则飞书会判超时重推。
    """

    name: str = "base"

    @abstractmethod
    def send_text(
        self,
        *,
        chat_id: str,
        text: str,
        reply_to_message_id: str | None = None,
        thread_id: str | None = None,
    ) -> SendResult:
        raise NotImplementedError

    @abstractmethod
    def run_forever(self, on_message: MessageHandler) -> None:
        raise NotImplementedError

    # ---------------------------------------------------------------- 文件（可选能力）
    # 默认实现返回 `unsupported_channel`：不支持的渠道显式失败，而不是静默丢文件。
    def send_image(
        self,
        *,
        chat_id: str,
        path: str,
        reply_to_message_id: str | None = None,
    ) -> SendResult:
        """发送一张本地图片；失败用 `SendResult` 表达，不抛异常。"""
        return SendResult(ok=False, error="unsupported_channel")

    def send_file(
        self,
        *,
        chat_id: str,
        path: str,
        name: str = "",
        reply_to_message_id: str | None = None,
    ) -> SendResult:
        """发送一个本地文件；失败用 `SendResult` 表达，不抛异常。"""
        return SendResult(ok=False, error="unsupported_channel")
