"""图片内容块的模型能力适配（装配层）。

工具层（如 `Read`）**从不内联 base64**：checkpoint / 日志 / 消息表里只存一条
引用文本 `[图片] <路径> | <mime> | <n> 字节`（base64 会把 checkpoint 撑大几十倍）。
发给模型的请求由本模块按引用临时还原。

端点限制（DeepSeek Chat Completions，api-docs.deepseek.com/guides/vision）：
- 图片**只支持出现在 `user` 消息中**，tool / assistant 消息带图直接 400——
  所以还原后的 image 块不回填 ToolMessage，而是追加为一条独立的 user 消息；
- 格式仅 JPEG / PNG / GIF / WebP（BMP 等需转码）；单边 ≤8192px；base64 单图 ≤32MiB；
- 服务端本来就会把大图缩到 ~1300px 等效像素再计费（单图 token 上限 1024），
  预先缩到最长边 2048 只为减小请求体，视觉信息无损失。

两种模式（均不改 checkpoint 里的 state）：
- `inject_image_blocks`（supports_vision=True）：把引用还原成 user 消息里的 image 块；
- `adapt_messages`（supports_vision=False）：把 image 块换成等量文本占位，
  兼容历史 checkpoint 里可能残留的旧格式。
"""

from __future__ import annotations

import base64
import hashlib
import io
import re
import uuid
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage

DEFAULT_PLACEHOLDER = "[图片已省略：当前模型不支持视觉，原图仍在工作区，可换视觉模型后重读]"

_IMAGE_BLOCK_TYPES = frozenset({"image_url", "image"})

# 注入消息的识别标记（参考 deer-flow ViewImageMiddleware）：ID 前缀 + additional_kwargs
# 双重标记。middleware 每次请求先剥离带标记的旧消息再重建，防止历史 checkpoint 里
# 意外落盘的"搁浅副本"永远骑在请求上；Gateway 层会剥掉客户端输入里的该标记，
# 所以用户手写的同前缀文本不可能被误删。
_IMAGE_CONTEXT_ID_PREFIX = "vision-image-context:"
_IMAGE_CONTEXT_MARKER_KEY = "vision_image_context"

# fs.py 的图片分支用 format_image_reference 生成，这里用同一正则解析——
# 两端必须同步修改；mime 只认 image/* 前缀，防止标记文本被伪造注入。
_IMAGE_REF_RE = re.compile(r"^\[图片\] (.+?) \| (image/[\w.+-]+) \| (\d+) 字节$")

# 端点原生支持的格式（格式按文件实际内容判断，这里以工具声明的 mime 为准）。
_ENDPOINT_MIMES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})
# 预缩放阈值：端点硬限单边 8192px，服务端计费缩放等效 ~1300px；2048 远离所有限制。
_INJECT_MAX_EDGE = 2048
# 降级后仍过大的兜底阈值（base64 后超过就换 JPEG 再压一次）。
_INJECT_MAX_B64_CHARS = 6 * 1024 * 1024


def format_image_reference(path: Any, mime: str, size: int) -> str:
    """图片引用标记的唯一生成入口（fs.py 与解析正则共享格式）。"""
    return f"[图片] {path} | {mime} | {size} 字节"


def _is_image_block(block: Any) -> bool:
    return isinstance(block, dict) and str(block.get("type", "")) in _IMAGE_BLOCK_TYPES


def _load_image_data_url(path: str, mime: str) -> str | None:
    """读取图片文件并按端点限制降级；返回 data URL，提供不了图片时返回 None。

    - 转码：BMP 等端点不支持的格式 → PNG（GIF 动图取第一帧）；
    - 缩放：最长边 > 2048 → 等比缩到 2048；
    - 压缩：PNG 结果过大（base64 超 6MB 字符）→ 再压一版 JPEG（质量 85）；
    - Pillow 不可用或解码失败：受支持格式原样透传（超限由端点拒绝，引用文本仍在），
      不支持的格式返回 None（模型仍看得见引用标记，可自行处理）。
    """
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None

    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            img.load()
            needs_transcode = mime not in _ENDPOINT_MIMES
            needs_resize = max(img.size) > _INJECT_MAX_EDGE
            if not needs_transcode and not needs_resize:
                # 原生格式 + 尺寸合规：原样透传，避免重编码损失
                b64 = base64.b64encode(data).decode("ascii")
                return f"data:{mime};base64,{b64}"

            work = img
            if needs_resize:
                width, height = img.size
                scale = _INJECT_MAX_EDGE / max(img.size)
                work = img.resize(
                    (max(1, round(width * scale)), max(1, round(height * scale)))
                )
            # 有透明通道的转 PNG，其余转 JPEG（体积小得多）
            out_mime = "image/png" if work.mode in ("RGBA", "LA", "P", "PA") else "image/jpeg"
            buf = io.BytesIO()
            work.convert("RGBA" if out_mime == "image/png" else "RGB").save(buf, format=out_mime.split("/")[1].upper())
            encoded = buf.getvalue()
            if out_mime == "image/png" and len(encoded) * 4 // 3 > _INJECT_MAX_B64_CHARS:
                buf = io.BytesIO()
                work.convert("RGB").save(buf, format="JPEG", quality=85)
                encoded = buf.getvalue()
                out_mime = "image/jpeg"
            b64 = base64.b64encode(encoded).decode("ascii")
            return f"data:{out_mime};base64,{b64}"
    except ImportError:
        # 无 Pillow：受支持格式原样透传，不支持的（bmp 等）只能放弃
        if mime in _ENDPOINT_MIMES:
            b64 = base64.b64encode(data).decode("ascii")
            return f"data:{mime};base64,{b64}"
        return None
    except Exception:  # noqa: BLE001 - 解码失败等一律退回引用文本
        return None


def is_vision_image_context(message: Any) -> bool:
    """判断一条消息是否为本模块注入的（可能搁浅的）图片上下文消息。"""
    return (
        isinstance(message, HumanMessage)
        and bool(getattr(message, "id", ""))
        and str(message.id).startswith(_IMAGE_CONTEXT_ID_PREFIX)
        and message.additional_kwargs.get(_IMAGE_CONTEXT_MARKER_KEY) is True
    )


# 每次请求最多还原的图片数：从**最新**引用往前数、按路径去重。
# 全量还原会让请求体随历史无界膨胀（每张 base64 数 MB，端点可能直接拒绝）；
# 更早的引用保持纯文本，模型需要时仍可重新 Read（引用标记一直都在）。
INJECT_MAX_IMAGES = 6


def inject_image_blocks(messages: list[Any]) -> list[Any]:
    """把最近 ≤INJECT_MAX_IMAGES 张（按路径 + 内容去重）图片引用还原成 user 消息。

    为什么不是回填进 ToolMessage：DeepSeek Chat Completions 只允许图片出现在
    user 消息里（tool role 带图 400），这也是 Claude Code 在 OpenAI 兼容端点上的做法。

    数量策略：从消息列表**末尾向前**扫描、按图片路径去重，凑满上限即停——
    保证最近读的图优先可见；更早的引用保持纯文本不转 base64，控制请求体体积。
    - 文件缺失 / 读取失败 / 转码失败：只保留原引用文本（模型仍看得见标记）；
    - **内容相同**（data URL 的 sha256 一致，与文件名无关）：只保留**最新**那一帧的位置，
      旧的那份丢掉——画面没变时不会重复占上下文，而"当前画面"永远排在最后；
    - 没有任何可用引用时返回原列表，不追加空消息。
    """
    # 从后往前收集引用：path -> 引用文本（insertion order = 从新到旧）
    picked: dict[str, str] = {}
    for message in reversed(messages):
        content = getattr(message, "content", None)
        if not isinstance(content, list):
            continue
        for block in reversed(content):
            if not (isinstance(block, dict) and block.get("type") == "text"):
                continue
            match = _IMAGE_REF_RE.match(str(block.get("text", "")).strip())
            if not match or match.group(1) in picked:
                continue
            picked[match.group(1)] = match.group(0)
            if len(picked) >= INJECT_MAX_IMAGES:
                break
        if len(picked) >= INJECT_MAX_IMAGES:
            break
    if not picked:
        return messages

    # 转成正序（从旧到新）还原，与对话出现顺序一致
    body: list[Any] = []
    frame_digests: dict[str, int] = {}
    merged = 0
    for _path, ref_text in reversed(list(picked.items())):
        match = _IMAGE_REF_RE.match(ref_text)
        if not match:
            continue
        url = _load_image_data_url(match.group(1), match.group(2))
        if not url:
            continue
        # 判重对象是**实际要发出去的图**（data URL），比按文件字节更贴近结果：
        # 同一内容即使落在不同文件名上，编码后也完全一致。
        digest = hashlib.sha256(url.encode("ascii")).hexdigest()
        if digest in frame_digests:
            # 内容重复：删掉旧位置那份，把最新一帧放回时间线末尾（=当前画面）
            old = frame_digests.pop(digest)
            body[old : old + 2] = []
            frame_digests = {
                key: (idx - 2 if idx > old else idx) for key, idx in frame_digests.items()
            }
            merged += 1
        frame_digests[digest] = len(body)
        body.append({"type": "text", "text": ref_text})
        body.append({"type": "image_url", "image_url": {"url": url}})
    if not body:
        return messages  # 收集到了引用但一张都没能还原，不追加空消息
    # 计数按**实际还原**的张数：引用还在但文件已被清理（CUA 只留最近 3 张）时，
    # 不能把没加载的引用算进去，否则标题数字与后面的图对不上。
    loaded = len(body) // 2
    dedup_note = (
        f"另有 {merged} 帧与上述图片内容完全相同（画面没有变化），已合并、不重复占位。"
        if merged
        else ""
    )
    blocks: list[Any] = [
        {
            "type": "text",
            "text": (
                f"[图片还原] 以下是上方工具结果中引用的**最近 {loaded} 张**图片"
                f"（单次最多 {INJECT_MAX_IMAGES} 张，更早的引用保持文本，需要时请重新读取）。"
                f"{dedup_note}按顺序对应其来源标记："
            ),
        },
        *body,
    ]
    context = HumanMessage(
        id=f"{_IMAGE_CONTEXT_ID_PREFIX}{uuid.uuid4().hex}",
        content=blocks,
        additional_kwargs={"vision_image_context": True},
    )
    return [*messages, context]


def adapt_content(
    content: Any,
    *,
    supports_vision: bool,
    placeholder: str = DEFAULT_PLACEHOLDER,
) -> Any:
    """转换单条消息 content；非列表或支持视觉时原样返回。"""
    if supports_vision or not isinstance(content, list):
        return content
    out: list[Any] = []
    for block in content:
        if _is_image_block(block):
            out.append({"type": "text", "text": placeholder})
        else:
            out.append(block)
    return out


def adapt_messages(
    messages: list[Any],
    *,
    supports_vision: bool,
    placeholder: str = DEFAULT_PLACEHOLDER,
) -> list[Any]:
    """返回新的消息列表（不修改入参）；不支持视觉时逐条降级 image 块。

    兼容 langchain BaseMessage 的 `model_copy(update=...)`；
    对没有该方法的对象保持原样，避免在本层引入框架依赖。
    """
    if supports_vision:
        return messages
    out: list[Any] = []
    for message in messages:
        content = getattr(message, "content", None)
        if not isinstance(content, list):
            out.append(message)
            continue
        adapted = adapt_content(content, supports_vision=False, placeholder=placeholder)
        copier = getattr(message, "model_copy", None)
        out.append(copier(update={"content": adapted}) if callable(copier) else message)
    return out
