"""渠道抽象层（**接口冻结，由主线维护**）。

飞书 / 控制台都实现 `Channel`。gateway 只依赖这三个符号，不 import 具体实现。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable

from contracts import IncomingMessage, SendResult


MessageHandler = Callable[[IncomingMessage], None]


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
