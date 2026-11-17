"""截图与坐标空间（§8.3）。

只有一套坐标空间：模型看到的是长边缩到 2048 的那张图，模型回传的坐标就是
这张图的像素坐标；运行时 `physical = round(model * scale)`。

`scale` 由物理长边 / 2048 算出（本机 3072/2048 = 1.5，精确），**不是**读系统
缩放比例得到的——DPI 已在进程启动时声明（`cua.dpi`），两者不混用。

编码参数与 `agents/vision.py` 的注入管线一致（BICUBIC + JPEG q75），这样注入
时图片会被原样透传（尺寸合规、格式受支持），不会再压一次。
"""

from __future__ import annotations

import hashlib
import io
import os
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageStat

from cua import _win32

# 与 agents/vision.py 的 `_INJECT_MAX_EDGE` 对齐；改一处必须同时改另一边。
MODEL_MAX_EDGE = 2048
JPEG_QUALITY = 75

_RESAMPLE = getattr(getattr(Image, "Resampling", Image), "BICUBIC")


@dataclass(frozen=True, slots=True)
class Geometry:
    """单屏坐标空间。`scale` 是 model → physical 的乘数。"""

    phys_w: int
    phys_h: int
    model_w: int
    model_h: int
    scale: float
    origin: tuple[int, int] = (0, 0)

    def as_dict(self) -> dict:
        return {
            "width": self.model_w,
            "height": self.model_h,
            "scale": self.scale,
            "origin": list(self.origin),
            "physical": [self.phys_w, self.phys_h],
        }

    def to_physical(self, x: float, y: float) -> tuple[int, int]:
        return round(x * self.scale), round(y * self.scale)

    def to_model(self, px: float, py: float) -> tuple[int, int]:
        return round(px / self.scale), round(py / self.scale)


_GEOM: Geometry | None = None


def geometry(refresh: bool = False) -> Geometry:
    """当前主屏几何；进程内缓存（单屏、不处理拔插）。"""
    global _GEOM
    if _GEOM is not None and not refresh:
        return _GEOM
    phys_w, phys_h = _win32.primary_size()
    if phys_w <= 0 or phys_h <= 0:
        raise OSError("主屏尺寸为 0（会话可能未激活或处于锁屏/UAC）")
    edge = max(phys_w, phys_h)
    scale = (edge / MODEL_MAX_EDGE) if edge > MODEL_MAX_EDGE else 1.0
    model_w = max(1, round(phys_w / scale))
    model_h = max(1, round(phys_h / scale))
    # 截图从主屏 (0,0) 开始抓，所以模型坐标原点恒为 (0,0)。
    # 虚拟桌面原点可能是负数（副屏在主屏上方/左侧），那只影响 SendInput 的归一化，
    # 不影响截图坐标空间（单屏口径，见 §8.3）。
    _GEOM = Geometry(
        phys_w=phys_w,
        phys_h=phys_h,
        model_w=model_w,
        model_h=model_h,
        scale=round(scale, 6),
        origin=(0, 0),
    )
    return _GEOM


def _to_image(raw: bytes, width: int, height: int) -> Image.Image:
    return Image.frombuffer("RGBA", (width, height), raw, "raw", "BGRA", 0, 1).convert("RGB")


def _resize_to_model(img: Image.Image, geom: Geometry) -> Image.Image:
    if img.size == (geom.model_w, geom.model_h):
        return img
    return img.resize((geom.model_w, geom.model_h), _RESAMPLE)


def _encode_jpeg(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _save_view(img: Image.Image, path: Path) -> dict:
    data = _encode_jpeg(img)
    _write_atomic(path, data)
    luma = ImageStat.Stat(img.convert("L")).mean[0]
    return {
        "path": str(path),
        "mime": "image/jpeg",
        "bytes": len(data),
        "width": img.width,
        "height": img.height,
        "sha256": hashlib.sha256(data).hexdigest()[:16],
        "mean_luma": round(float(luma), 2),
    }


def capture_full() -> Image.Image:
    """整屏（主屏物理分辨率）截图，返回未缩放的 RGB 图。"""
    geom = geometry()
    raw = _win32.grab_bgra(0, 0, geom.phys_w, geom.phys_h)
    return _to_image(raw, geom.phys_w, geom.phys_h)


def capture_view(path: Path) -> dict:
    """整屏取景 → 缩到模型空间 → 存 JPEG；返回图元信息。"""
    geom = geometry()
    view = _resize_to_model(capture_full(), geom)
    info = _save_view(view, path)
    info.update({"scale": geom.scale, "origin": list(geom.origin)})
    return info


def capture_region(region: tuple[int, int, int, int], path: Path) -> dict:
    """按模型空间 region 裁剪，1:1 返回（长边仍受 2048 限制）。

    坐标仍按整图空间表达（与 Anthropic 的 zoom 语义对齐：zoom 不改变坐标系）。
    """
    geom = geometry()
    x0, y0, x1, y1 = region
    px0, py0 = geom.to_physical(x0, y0)
    px1, py1 = geom.to_physical(x1, y1)
    px0 = max(0, min(px0, geom.phys_w - 1))
    py0 = max(0, min(py0, geom.phys_h - 1))
    px1 = max(px0 + 1, min(px1, geom.phys_w))
    py1 = max(py0 + 1, min(py1, geom.phys_h))

    crop = capture_full().crop((px0, py0, px1, py1))
    crop_scale = 1.0
    if max(crop.size) > MODEL_MAX_EDGE:
        crop_scale = max(crop.size) / MODEL_MAX_EDGE
        crop = crop.resize(
            (max(1, round(crop.width / crop_scale)), max(1, round(crop.height / crop_scale))),
            _RESAMPLE,
        )
    info = _save_view(crop, path)
    # 这张图里 1 个像素对应多少物理像素（1.0 = 原生 1:1）
    info.update({"scale": round(crop_scale, 6), "origin": list(geom.origin)})
    return info
