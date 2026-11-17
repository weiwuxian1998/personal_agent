"""飞书渠道实现（M-CHANNEL）。

职责边界（契约 §2 / §6.2）：
- 本文件只负责「收」与「发」：长连接收事件 → 解析成 `IncomingMessage` → 进程内去重
  → 交给 `on_message` 回调；`send_text` 调 OpenAPI 发纯文本。
- **不做** Redis XADD、LLM、数据库等业务动作：这些属于 gateway / router，回调必须毫秒级返回。

已在本机 lark-oapi==1.7.5 源码中核实的事实：
1. 长连接：`from lark_oapi.ws import Client`（顶层导出为 `lark.ws.Client`）；
   `client.start()` 阻塞当前线程直到进程结束。
2. 事件分发：长连接模式下 `EventDispatcherHandler.builder("", "")` 的两个参数必须为空字符串
   （WS 已做过加密与鉴权，不需要 encrypt_key / verification_token）。
3. ACK：SDK 同步调用回调，回调返回后才回 ACK；回调抛异常 → SDK 回 500 → 飞书重推。
   因此回调必须吞异常，且不能做耗时操作。
4. 发送：`client.im.v1.message.reply(ReplyMessageRequest)`（按 message_id 回复）或
   `client.im.v1.message.create(CreateMessageRequest)`（按 chat_id 发送），
   返回 `SendResult`，**永不抛异常给调用方**。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateFileRequest,
    CreateFileRequestBody,
    CreateImageRequest,
    CreateImageRequestBody,
    CreateMessageRequest,
    CreateMessageRequestBody,
    P2ImMessageReceiveV1,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

from channel.base import Channel, MessageHandler
from contracts import IncomingMessage, SendResult

logger = logging.getLogger(__name__)

# 单条文本消息的保守上限（字符）。飞书文本内容过大时接口会报错，
# v1 取 4000 字符作为安全边际；超出部分按段落/换行/标点优先切成多条。
DEFAULT_MAX_TEXT_CHARS = 4000

# 进程内去重窗口：只防飞书 ACK 失败导致的重推；跨进程幂等仍由 gateway 的 Redis SETNX 负责。
DEFAULT_DEDUPE_TTL_S = 60.0

# 切分优先级：段落 > 换行 > 句末标点 > 逗号/顿号 > 空白 > 硬切
_BREAK_SEPARATORS = ("\n\n", "\n", "。", "；", "！", "？", "，", "、", " ", "\t")

# 发文件上限（2026-11 真实上传实测）：
# images 10 MiB 通过、10 MiB+1 报 234006；files 27,000,000 字节通过、27.5MB 起失败，取 25 MiB。
DEFAULT_IMAGE_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_FILE_MAX_BYTES = 25 * 1024 * 1024
IMAGE_SUFFIXES: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".tif", ".tiff"}
)


def _to_int(value: Any, default: int = 0) -> int:
    """把飞书返回的毫秒时间戳（可能是字符串）安全地转成 int。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def split_text(text: str, *, limit: int = DEFAULT_MAX_TEXT_CHARS) -> list[str]:
    """把长文本切成若干段，保证 `"".join(结果) == 原文`（不丢字、不重复）。

    切点优先级：空行 → 换行 → 句末标点 → 逗号/顿号 → 空白 → 硬切。
    空文本返回 `[]`；长度不超过 `limit` 时原样返回单段。
    """
    if limit <= 0:
        raise ValueError("limit 必须为正整数")
    text = text or ""
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut = limit
        for sep in _BREAK_SEPARATORS:
            idx = window.rfind(sep)
            if idx > 0:
                cut = idx + len(sep)
                break
        chunks.append(rest[:cut])
        rest = rest[cut:]
    if rest:
        chunks.append(rest)
    return chunks


class MessageDeduper:
    """进程内 TTL 去重表（线程安全）。

    用于挡住飞书因 ACK 超时/失败产生的重复投递；`message_id` 是官方要求的幂等键，
    缺省时退回 `event_id`。跨进程去重仍由 gateway 的 `SET NX` 负责，两者互不冲突。

    回调处理失败时可调用 `forget(key)`，让下一次重推仍能被处理（避免消息丢失）。
    """

    def __init__(self, *, ttl_s: float = DEFAULT_DEDUPE_TTL_S, max_entries: int = 1024) -> None:
        self._ttl_s = max(float(ttl_s), 0.0)
        self._max_entries = max(int(max_entries), 1)
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def seen(self, key: str | None) -> bool:
        """登记并判断：首次返回 False，窗口内重复返回 True；空 key 视为不重复。"""
        if not key:
            return False
        now = time.monotonic()
        with self._lock:
            self._evict(now)
            if key in self._seen:
                return True
            self._seen[key] = now
            while len(self._seen) > self._max_entries:
                self._seen.pop(next(iter(self._seen)))
            return False

    def forget(self, key: str | None) -> None:
        if not key:
            return
        with self._lock:
            self._seen.pop(key, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._seen)

    def _evict(self, now: float) -> None:
        if not self._ttl_s:
            self._seen.clear()
            return
        expired = [key for key, ts in self._seen.items() if now - ts >= self._ttl_s]
        for key in expired:
            del self._seen[key]


def parse_p2_message(
    data: Optional[P2ImMessageReceiveV1],
    *,
    download: Any = None,
) -> Optional[IncomingMessage]:
    """把飞书 `im.message.receive_v1` 事件转成平台无关的 `IncomingMessage`（契约 §6.2）。

    过滤规则：
    - 只收 `sender_type in (None, "user")`（过滤机器人/应用自身消息，避免自问自答）；
    - `text` 直接取正文；`image` / `file` / `audio` / `media` 通过 `download` 回调落盘，
      文本里写本地路径（`Read` 能读图，VisionAdapter 负责多模态）；
    - `download=None` 或下载失败 → 记日志后跳过（不影响纯文本链路）；
    - `content` 解析失败按空文本/无附件处理；
    - 群聊文本里的 `@_user_1` 占位符按 `mentions[].key` 删除；
    - 清洗后为空 → 返回 None（例如只在群里 @ 了机器人、没说话）。
    """
    if data is None:
        return None
    event = getattr(data, "event", None)
    message = getattr(event, "message", None)
    if message is None:
        return None

    sender = getattr(event, "sender", None)
    sender_type = getattr(sender, "sender_type", None)
    if sender_type not in (None, "user"):
        logger.debug("跳过非用户消息 sender_type=%s", sender_type)
        return None

    message_id = getattr(message, "message_id", "") or ""
    message_type = getattr(message, "message_type", None)
    if message_type not in ("text", "image", "file", "audio", "media"):
        logger.info("暂不支持的消息类型 message_type=%s message_id=%s", message_type, message_id)
        return None

    content = getattr(message, "content", "") or ""
    try:
        body = json.loads(content) if content else {}
    except (json.JSONDecodeError, AttributeError, TypeError):
        logger.warning("消息 content 不是合法 JSON，按空文本处理 message_id=%s", message_id)
        body = {}
    if not isinstance(body, dict):
        body = {}

    attachment = ""
    if message_type == "text":
        text = str(body.get("text") or "")
        mentions = getattr(message, "mentions", None) or []
        for mention in mentions:
            key = getattr(mention, "key", None)
            if key:
                text = text.replace(key, "")
        text = text.strip()
        if not text:
            return None
    else:
        resource_key = str(body.get("image_key") or body.get("file_key") or "")
        file_name = str(body.get("file_name") or "")
        if not resource_key or download is None:
            logger.info(
                "附件消息无法落盘 message_type=%s message_id=%s（无 key 或无下载器）",
                message_type, message_id,
            )
            return None
        attachment = str(download(message_id, resource_key, message_type, file_name) or "")
        if not attachment:
            logger.warning("附件下载失败 message_type=%s message_id=%s", message_type, message_id)
            return None
        if message_type == "image":
            text = f"[图片] {attachment}（本地路径，可用 Read 查看）"
        elif message_type == "file":
            text = f"[文件] {file_name or '未命名'} -> {attachment}"
        else:
            text = f"[{message_type}] -> {attachment}"

    sender_id_obj = getattr(sender, "sender_id", None)
    sender_id = getattr(sender_id_obj, "open_id", "") or ""

    header = getattr(data, "header", None)
    # 优先用消息自身的 create_time（会话时间），缺失时退回事件头的 create_time
    create_time = _to_int(getattr(message, "create_time", None)) or _to_int(
        getattr(header, "create_time", None)
    )

    return IncomingMessage(
        channel="feishu",
        chat_id=getattr(message, "chat_id", "") or "",
        chat_type=getattr(message, "chat_type", "") or "p2p",
        message_id=message_id,
        event_id=getattr(header, "event_id", "") or "",
        sender_id=sender_id,
        text=text,
        create_time=create_time,
        thread_id=getattr(message, "thread_id", None) or None,
        attachments=[attachment] if attachment else [],
        raw={"message_type": message_type, "content": content, "attachment": attachment},
    )


class FeishuChannel(Channel):
    """飞书实现：OpenAPI 发送 + WebSocket 长连接接收。"""

    name = "feishu"

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        domain: str = "https://open.feishu.cn",
        *,
        log_level: int = lark.LogLevel.INFO,
        max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
        dedupe_ttl_s: float = DEFAULT_DEDUPE_TTL_S,
        inbox_dir: Optional[str] = None,
        image_max_bytes: int = DEFAULT_IMAGE_MAX_BYTES,
        file_max_bytes: int = DEFAULT_FILE_MAX_BYTES,
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._domain = domain
        self._log_level = log_level
        self.max_text_chars = int(max_text_chars)
        self._dedupe_ttl_s = float(dedupe_ttl_s)
        self._image_max_bytes = int(image_max_bytes)
        self._file_max_bytes = int(file_max_bytes)
        self._inbox_dir = Path(inbox_dir) if inbox_dir else Path.home() / ".assistant" / "state" / "inbox"
        # OpenAPI 客户端（发消息用）；长连接客户端是另一个对象，在 run_forever 里创建。
        self._api = (
            lark.Client.builder()
            .app_id(app_id)
            .app_secret(app_secret)
            .domain(domain)
            .log_level(log_level)
            .build()
        )

    # ------------------------------------------------------------------ 收附件
    def _fetch_attachment(self, message_id: str, resource_key: str, kind: str, file_name: str = "") -> str:
        """下载图片/文件到 `inbox_dir`，返回本地绝对路径；失败返回空串（不抛）。"""
        try:
            from lark_oapi.api.im.v1 import GetMessageResourceRequest

            request = (
                GetMessageResourceRequest.builder()
                .message_id(message_id)
                .file_key(resource_key)
                .type(kind)
                .build()
            )
            response = self._api.im.v1.message_resource.get(request)
            if not response.success() or response.file is None:
                logger.warning(
                    "附件下载失败 message_id=%s kind=%s code=%s msg=%s",
                    message_id, kind, getattr(response, "code", ""), getattr(response, "msg", ""),
                )
                return ""
            payload = response.file.read()
            if isinstance(payload, str):
                payload = payload.encode()
            if not payload:
                return ""
            suffix = Path(file_name).suffix if file_name else ""
            if not suffix:
                suffix = ".png" if kind == "image" else ".bin"
            self._inbox_dir.mkdir(parents=True, exist_ok=True)
            target = self._inbox_dir / f"{uuid.uuid4().hex}{suffix}"
            target.write_bytes(payload)
            return str(target)
        except Exception:  # noqa: BLE001 - 下载失败不能影响长连接 ACK
            logger.warning("附件下载异常 message_id=%s kind=%s", message_id, kind, exc_info=True)
            return ""

    # ------------------------------------------------------------------ 发送
    def send_text(
        self,
        *,
        chat_id: str,
        text: str,
        reply_to_message_id: Optional[str] = None,
        thread_id: Optional[str] = None,
    ) -> SendResult:
        """发纯文本，失败不抛异常（统一用 `SendResult` 表达）。

        纯空白文本（`text.strip()` 为空）直接拒绝，避免发一条没有任何内容的空消息。

        - 文本超过 `max_text_chars` 时自动分段：
          - 有 `chat_id`：第 1 段用 reply（若给了 `reply_to_message_id`），其余段用 create；
          - 没有 `chat_id` 只有 `reply_to_message_id`：所有段都 reply 到同一条消息；
        - 任一段失败就停止后续发送，返回 `ok=False` 并带上失败段的 `code/error`；
        - 返回的 `message_id` 是**第 1 段**的 ID，供上层幂等记录使用；
        - `thread_id` 仅保留接口形状：飞书消息接口没有对应参数，忽略。
        """
        chunks = split_text(text, limit=self.max_text_chars)
        if not chunks or not (text or "").strip():
            logger.warning("拒绝发送空文本 chat_id=%s", chat_id)
            return SendResult(ok=False, error="empty_text")
        if not chat_id and not reply_to_message_id:
            logger.warning("缺少 chat_id / reply_to_message_id，无法发送")
            return SendResult(ok=False, error="missing_chat_id")

        first_message_id = ""
        for index, chunk in enumerate(chunks):
            use_reply = bool(reply_to_message_id) and (index == 0 or not chat_id)
            result = self._send_chunk(
                chat_id=chat_id,
                text=chunk,
                reply_to_message_id=reply_to_message_id,
                use_reply=use_reply,
                part=index + 1,
                parts=len(chunks),
            )
            if not result.ok:
                return SendResult(
                    ok=False,
                    message_id=first_message_id,
                    error=result.error,
                    code=result.code,
                )
            if index == 0:
                first_message_id = result.message_id
        return SendResult(ok=True, message_id=first_message_id)

    def _send_chunk(
        self,
        *,
        chat_id: str,
        text: str,
        reply_to_message_id: Optional[str],
        use_reply: bool,
        part: int,
        parts: int,
    ) -> SendResult:
        started = time.monotonic()
        try:
            content = json.dumps({"text": text}, ensure_ascii=False)
            if use_reply:
                body = (
                    ReplyMessageRequestBody.builder()
                    .content(content)
                    .msg_type("text")
                    .build()
                )
                request = (
                    ReplyMessageRequest.builder()
                    .message_id(reply_to_message_id or "")
                    .request_body(body)
                    .build()
                )
                response = self._api.im.v1.message.reply(request)
            else:
                body = (
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .msg_type("text")
                    .content(content)
                    .build()
                )
                request = (
                    CreateMessageRequest.builder()
                    .receive_id_type("chat_id")
                    .request_body(body)
                    .build()
                )
                response = self._api.im.v1.message.create(request)
        except Exception as exc:  # 网络异常等：不让调用方（router 工具）崩掉
            logger.exception("发送消息异常 chat_id=%s part=%s/%s", chat_id, part, parts)
            return SendResult(ok=False, error=f"{type(exc).__name__}: {exc}")

        duration_ms = int((time.monotonic() - started) * 1000)
        if not response.success():
            raw_code = getattr(response, "code", None)
            code = _to_int(raw_code, default=0) if raw_code is not None else None
            msg = getattr(response, "msg", "") or ""
            logger.warning(
                "发送失败 chat_id=%s part=%s/%s code=%s msg=%s duration_ms=%s",
                chat_id, part, parts, code, msg, duration_ms,
            )
            return SendResult(ok=False, error=f"code={code} msg={msg}", code=code)

        data = getattr(response, "data", None)
        message_id = getattr(data, "message_id", "") or ""
        logger.info(
            "发送成功 chat_id=%s part=%s/%s message_id=%s reply_to=%s duration_ms=%s",
            chat_id, part, parts, message_id, reply_to_message_id or "-", duration_ms,
        )
        return SendResult(ok=True, message_id=message_id)

    # ------------------------------------------------------------------ 发送文件
    def _send_resource_message(
        self,
        *,
        chat_id: str,
        msg_type: str,
        content: str,
        reply_to_message_id: Optional[str],
    ) -> SendResult:
        """用已上传的 `image_key` / `file_key` 发一条消息（图片或文件）。"""
        started = time.monotonic()
        try:
            if reply_to_message_id:
                body = (
                    ReplyMessageRequestBody.builder()
                    .content(content)
                    .msg_type(msg_type)
                    .build()
                )
                request = (
                    ReplyMessageRequest.builder()
                    .message_id(reply_to_message_id)
                    .request_body(body)
                    .build()
                )
                response = self._api.im.v1.message.reply(request)
            else:
                body = (
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .msg_type(msg_type)
                    .content(content)
                    .build()
                )
                request = (
                    CreateMessageRequest.builder()
                    .receive_id_type("chat_id")
                    .request_body(body)
                    .build()
                )
                response = self._api.im.v1.message.create(request)
        except Exception as exc:  # noqa: BLE001
            logger.exception("发送 %s 消息异常 chat_id=%s", msg_type, chat_id)
            return SendResult(ok=False, error=f"{type(exc).__name__}: {exc}")

        duration_ms = int((time.monotonic() - started) * 1000)
        if not response.success():
            raw_code = getattr(response, "code", None)
            code = _to_int(raw_code, default=0) if raw_code is not None else None
            msg = getattr(response, "msg", "") or ""
            logger.warning(
                "发送 %s 消息失败 chat_id=%s code=%s msg=%s duration_ms=%s",
                msg_type, chat_id, code, msg, duration_ms,
            )
            return SendResult(ok=False, error=f"code={code} msg={msg}", code=code)
        data = getattr(response, "data", None)
        logger.info(
            "发送 %s 消息成功 chat_id=%s message_id=%s duration_ms=%s",
            msg_type, chat_id, getattr(data, "message_id", ""), duration_ms,
        )
        return SendResult(ok=True, message_id=getattr(data, "message_id", "") or "")

    def _precheck(self, path: str, *, max_bytes: int, kind: str) -> tuple[Path | None, str]:
        """本地预检：绝对路径 + 存在 + 大小 + 图片后缀。返回 `(path, 错误码)`。"""
        target = Path(path)
        if not target.is_absolute():
            return None, "path_not_absolute"
        try:
            size = target.stat().st_size
        except OSError:
            return None, "not_found"
        if not target.is_file():
            return None, "not_a_file"
        if size > max_bytes:
            return None, f"oversized_{kind}"
        if kind == "image" and target.suffix.lower() not in IMAGE_SUFFIXES:
            return None, "unsupported_image_type"
        return target, ""

    def send_image(
        self,
        *,
        chat_id: str,
        path: str,
        reply_to_message_id: Optional[str] = None,
    ) -> SendResult:
        """上传本地图片（`im/v1/images`）→ 发 `image` 消息；失败不抛异常。"""
        target, reason = self._precheck(path, max_bytes=self._image_max_bytes, kind="image")
        if target is None:
            logger.warning("图片预检失败 path=%s reason=%s", path, reason)
            return SendResult(ok=False, error=reason)
        try:
            with open(target, "rb") as fh:
                request = (
                    CreateImageRequest.builder()
                    .request_body(
                        CreateImageRequestBody.builder()
                        .image(fh)
                        .image_type("message")
                        .build()
                    )
                    .build()
                )
                response = self._api.im.v1.image.create(request)
        except Exception as exc:  # noqa: BLE001
            logger.exception("上传图片异常 path=%s", path)
            return SendResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        if not response.success():
            raw_code = getattr(response, "code", None)
            code = _to_int(raw_code, default=0) if raw_code is not None else None
            msg = getattr(response, "msg", "") or ""
            logger.warning("上传图片失败 path=%s code=%s msg=%s", path, code, msg)
            return SendResult(ok=False, error=f"upload_failed code={code} msg={msg}", code=code)
        image_key = getattr(getattr(response, "data", None), "image_key", "") or ""
        if not image_key:
            return SendResult(ok=False, error="empty_image_key")
        return self._send_resource_message(
            chat_id=chat_id,
            msg_type="image",
            content=json.dumps({"image_key": image_key}, ensure_ascii=False),
            reply_to_message_id=reply_to_message_id,
        )

    def send_file(
        self,
        *,
        chat_id: str,
        path: str,
        name: str = "",
        reply_to_message_id: Optional[str] = None,
    ) -> SendResult:
        """上传本地文件（`im/v1/files`）→ 发 `file` 消息；失败不抛异常。"""
        target, reason = self._precheck(path, max_bytes=self._file_max_bytes, kind="file")
        if target is None:
            logger.warning("文件预检失败 path=%s reason=%s", path, reason)
            return SendResult(ok=False, error=reason)
        display_name = (name or target.name).strip() or target.name
        try:
            with open(target, "rb") as fh:
                request = (
                    CreateFileRequest.builder()
                    .request_body(
                        CreateFileRequestBody.builder()
                        .file(fh)
                        .file_type("stream")
                        .file_name(display_name)
                        .build()
                    )
                    .build()
                )
                response = self._api.im.v1.file.create(request)
        except Exception as exc:  # noqa: BLE001
            logger.exception("上传文件异常 path=%s", path)
            return SendResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        if not response.success():
            raw_code = getattr(response, "code", None)
            code = _to_int(raw_code, default=0) if raw_code is not None else None
            msg = getattr(response, "msg", "") or ""
            logger.warning("上传文件失败 path=%s code=%s msg=%s", path, code, msg)
            return SendResult(ok=False, error=f"upload_failed code={code} msg={msg}", code=code)
        file_key = getattr(getattr(response, "data", None), "file_key", "") or ""
        if not file_key:
            return SendResult(ok=False, error="empty_file_key")
        return self._send_resource_message(
            chat_id=chat_id,
            msg_type="file",
            content=json.dumps({"file_key": file_key}, ensure_ascii=False),
            reply_to_message_id=reply_to_message_id,
        )

    # ------------------------------------------------------------------ 接收
    def build_dispatcher(self, on_message: MessageHandler) -> "lark.EventDispatcherHandler":
        """构造事件分发器；`on_message` 必须是毫秒级返回的轻函数。

        包装层只做三件事：记原始事件日志 → 解析 → 进程内去重，然后调用 `on_message`。
        任何异常都在这里吞掉并记日志，避免 SDK 回 500 触发飞书无限重推。
        """
        deduper = MessageDeduper(ttl_s=self._dedupe_ttl_s)

        def _on_p2_im_message_receive_v1(data: P2ImMessageReceiveV1) -> None:
            try:
                header = getattr(data, "header", None)
                event = getattr(data, "event", None)
                message = getattr(event, "message", None)
                sender = getattr(event, "sender", None)
                logger.info(
                    "收到原始事件 event_type=%s event_id=%s sender_type=%s "
                    "message_type=%s message_id=%s chat_id=%s chat_type=%s",
                    getattr(header, "event_type", None),
                    getattr(header, "event_id", None),
                    getattr(sender, "sender_type", None),
                    getattr(message, "message_type", None),
                    getattr(message, "message_id", None),
                    getattr(message, "chat_id", None),
                    getattr(message, "chat_type", None),
                )

                incoming = parse_p2_message(data, download=self._fetch_attachment)
                if incoming is None:
                    return

                dedupe_key = incoming.message_id or incoming.event_id
                if deduper.seen(dedupe_key):
                    logger.info(
                        "重复投递，已忽略 message_id=%s event_id=%s",
                        incoming.message_id, incoming.event_id,
                    )
                    return

                try:
                    on_message(incoming)
                except Exception:
                    # 交给 gateway 后会 Redis SETNX + XADD；失败时释放本地去重键，
                    # 让飞书的下一次重推仍有机会被处理。
                    deduper.forget(dedupe_key)
                    logger.exception(
                        "on_message 回调失败（已释放本地去重） message_id=%s",
                        incoming.message_id,
                    )
            except Exception:
                logger.exception("处理飞书事件失败，已忽略（避免触发飞书重推）")

        return (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(_on_p2_im_message_receive_v1)
            .build()
        )

    def run_forever(self, on_message: MessageHandler) -> None:
        """启动长连接并阻塞当前线程；断线重连由 SDK 内部处理。"""
        dispatcher = self.build_dispatcher(on_message)
        ws_client = lark.ws.Client(
            self._app_id,
            self._app_secret,
            event_handler=dispatcher,
            log_level=self._log_level,
            domain=self._domain,
            auto_reconnect=True,
        )

        def _on_reconnecting() -> None:
            logger.warning("飞书长连接断开，SDK 正在重连…")

        def _on_reconnected() -> None:
            logger.info("飞书长连接已恢复")

        ws_client.on_reconnecting = _on_reconnecting
        ws_client.on_reconnected = _on_reconnected

        logger.info("正在建立飞书长连接（不需要公网 IP）…")
        try:
            ws_client.start()  # 阻塞；断线重连在 SDK 内部完成
        except Exception:
            logger.exception("飞书长连接异常退出")
            raise
        logger.warning("飞书长连接已退出")
