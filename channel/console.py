"""控制台打桩渠道（M-CHANNEL）。

用途：没有飞书凭证 / 不想打扰手机时，用 stdin/stdout 跑通「收 → 处理 → 发」闭环。
契约 §6.3：
- `run_forever` 从 stdin 逐行读入，构造 `IncomingMessage(channel="console", chat_id="console", ...)`；
- `send_text` 打印到 stdout，固定返回 `SendResult(ok=True, message_id=ulid())`。
"""

from __future__ import annotations

import sys
import time
from typing import Optional

from channel.base import Channel, MessageHandler
from contracts import IncomingMessage, SendResult, ulid

# 输入这两行（忽略大小写与首尾空白）时退出，便于交互式调试；管道 EOF 也会退出。
EXIT_COMMANDS = frozenset({"/quit", "/exit", "exit", "quit"})


class ConsoleChannel(Channel):
    name = "console"

    def __init__(self, *, chat_id: str = "console", sender_id: str = "console") -> None:
        self.chat_id = chat_id
        self.sender_id = sender_id
        # 自检脚本与调试代码用它断言「发了什么」
        self.sent: list[dict] = []

    def send_text(
        self,
        *,
        chat_id: str,
        text: str,
        reply_to_message_id: Optional[str] = None,
        thread_id: Optional[str] = None,
    ) -> SendResult:
        message_id = ulid()
        self.sent.append(
            {
                "chat_id": chat_id,
                "text": text,
                "reply_to_message_id": reply_to_message_id,
                "thread_id": thread_id,
                "message_id": message_id,
            }
        )
        print(f"[console-send] chat_id={chat_id} reply_to={reply_to_message_id or '-'}")
        print(text)
        print()
        return SendResult(ok=True, message_id=message_id)

    def send_image(
        self,
        *,
        chat_id: str,
        path: str,
        reply_to_message_id: Optional[str] = None,
    ) -> SendResult:
        return self._send_path(chat_id=chat_id, path=path, kind="image", reply_to_message_id=reply_to_message_id)

    def send_file(
        self,
        *,
        chat_id: str,
        path: str,
        name: str = "",
        reply_to_message_id: Optional[str] = None,
    ) -> SendResult:
        return self._send_path(chat_id=chat_id, path=path, kind="file", reply_to_message_id=reply_to_message_id)

    def _send_path(
        self,
        *,
        chat_id: str,
        path: str,
        kind: str,
        reply_to_message_id: Optional[str],
    ) -> SendResult:
        """控制台没有附件通道：把路径打印出来，并记录到 `sent` 供自检断言。"""
        message_id = ulid()
        self.sent.append(
            {
                "chat_id": chat_id,
                "text": "",
                "kind": kind,
                "path": path,
                "reply_to_message_id": reply_to_message_id,
                "message_id": message_id,
            }
        )
        print(f"[console-send] {kind} {path}")
        return SendResult(ok=True, message_id=message_id)

    def run_forever(self, on_message: MessageHandler) -> None:
        """逐行读 stdin；空行跳过，EOF / Ctrl+C / quit 命令时返回。"""
        while True:
            try:
                line = sys.stdin.readline()
            except (KeyboardInterrupt, EOFError):
                return
            if not line:  # EOF
                return
            text = line.rstrip("\r\n")
            if not text.strip():
                continue
            if text.strip().lower() in EXIT_COMMANDS:
                return
            on_message(
                IncomingMessage(
                    channel="console",
                    chat_id=self.chat_id,
                    chat_type="p2p",
                    message_id=ulid(),
                    event_id=ulid(),
                    sender_id=self.sender_id,
                    text=text,
                    create_time=int(time.time() * 1000),
                    thread_id=None,
                    raw={},
                )
            )
