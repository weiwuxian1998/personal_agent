"""输入注入（SendInput）：鼠标、滚轮、Unicode 文本、组合键。

实现路径取自 `cua-desktop-bench/input_inject.py`（本机实测：绝对移动误差 0–3 px、
单击/双击/右键/滚轮/Unicode 键入全部命中、光标可还原）。

只用 `KEYEVENTF_UNICODE` 打字；`key` 走 VK，但本机中文输入法会把 VK 吞成 229
（根因【未验证】，见 §8.6），所以**发送前先检测 IME**，开着就如实返回
`ime_intercept`，不做"发了但没生效"的假装成功。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import time

from cua import _win32 as w

imm32 = ctypes.WinDLL("imm32", use_last_error=True)
imm32.ImmGetContext.restype = ctypes.c_void_p
imm32.ImmGetContext.argtypes = [w.HANDLE]
imm32.ImmGetOpenStatus.restype = ctypes.c_int
imm32.ImmGetOpenStatus.argtypes = [ctypes.c_void_p]
imm32.ImmReleaseContext.restype = ctypes.c_int
imm32.ImmReleaseContext.argtypes = [w.HANDLE, ctypes.c_void_p]

_BUTTONS = {
    "left": (w.MOUSEEVENTF_LEFTDOWN, w.MOUSEEVENTF_LEFTUP),
    "right": (w.MOUSEEVENTF_RIGHTDOWN, w.MOUSEEVENTF_RIGHTUP),
    "middle": (w.MOUSEEVENTF_MIDDLEDOWN, w.MOUSEEVENTF_MIDDLEUP),
}

_VK = {
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "esc": 0x1B,
    "escape": 0x1B,
    "backspace": 0x08,
    "delete": 0x2E,
    "space": 0x20,
    "insert": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "win": 0x5B,
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "shift": 0x10,
}
for _i in range(1, 13):
    _VK[f"f{_i}"] = 0x6F + _i  # F1=0x70

_MODIFIERS = {"ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B}
_EXTENDED_VKS = {0x2E, 0x24, 0x23, 0x21, 0x22, 0x25, 0x26, 0x27}


def _abs_xy(x: int, y: int) -> tuple[int, int]:
    vx, vy, vcx, vcy = w.virtual_desktop()
    nx = int(round((x - vx) * 65535.0 / max(1, vcx - 1)))
    ny = int(round((y - vy) * 65535.0 / max(1, vcy - 1)))
    return nx, ny


def cursor_pos() -> tuple[int, int]:
    pt = wt.POINT()
    w.user32.GetCursorPos(ctypes.byref(pt))
    return int(pt.x), int(pt.y)


def move_to(x: int, y: int) -> dict:
    """绝对移动到物理坐标；回来时读一次真实位置，delta 是事实不是假设。"""
    nx, ny = _abs_xy(x, y)
    res = w.send_input(
        w.mouse_input(
            w.MOUSEEVENTF_MOVE | w.MOUSEEVENTF_ABSOLUTE | w.MOUSEEVENTF_VIRTUALDESK,
            nx,
            ny,
        )
    )
    time.sleep(0.02)
    reached = cursor_pos()
    return {
        "accepted": res["accepted"],
        "reached": [reached[0], reached[1]],
        "delta": [reached[0] - x, reached[1] - y],
    }


def click_at(x: int, y: int, *, button: str = "left", count: int = 1) -> dict:
    down, up = _BUTTONS[button]
    moved = move_to(x, y)
    for i in range(max(1, count)):
        w.send_input(w.mouse_input(down))
        time.sleep(0.01)
        w.send_input(w.mouse_input(up))
        if i + 1 < count:
            time.sleep(0.06)  # 双击间隔低于系统阈值才算同一次双击
    return {"at": [x, y], "moved": moved, "count": max(1, count)}


def drag(from_xy: tuple[int, int], to_xy: tuple[int, int], *, duration_ms: int = 300) -> dict:
    (x0, y0), (x1, y1) = from_xy, to_xy
    move_to(x0, y0)
    w.send_input(w.mouse_input(w.MOUSEEVENTF_LEFTDOWN))
    steps = max(1, min(60, duration_ms // 16))
    start = time.perf_counter()
    try:
        for i in range(1, steps + 1):
            t = i / steps
            move_to(round(x0 + (x1 - x0) * t), round(y0 + (y1 - y0) * t))
            time.sleep(0.016)
    finally:
        w.send_input(w.mouse_input(w.MOUSEEVENTF_LEFTUP))
    return {
        "from": [x0, y0],
        "to": [x1, y1],
        "elapsed_ms": round((time.perf_counter() - start) * 1000, 1),
    }


def type_text(text: str) -> dict:
    """按 Unicode 逐字符键入（不受输入法影响）。"""
    items: list[w.INPUT] = []
    for ch in text:
        code = ord(ch)
        # 代理对（emoji 等）按 UTF-16 两个 code unit 发，Windows 才能正确组合。
        if code > 0xFFFF:
            code -= 0x10000
            units = (0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF))
        else:
            units = (code,)
        for unit in units:
            items.append(w.key_input(0, unit, w.KEYEVENTF_UNICODE))
            items.append(w.key_input(0, unit, w.KEYEVENTF_UNICODE | w.KEYEVENTF_KEYUP))
    if not items:
        return {"chars": 0, "accepted": 0}
    res = w.send_input(*items)
    return {"chars": len(text), "accepted": res["accepted"]}


_w_user32 = w.user32


def ime_open() -> bool | None:
    """前台窗口的输入法是否处于打开状态；拿不到上下文时返回 None。"""
    hwnd = _w_user32.GetForegroundWindow()
    if not hwnd:
        return None
    ctx = imm32.ImmGetContext(hwnd)
    if not ctx:
        return None
    try:
        return bool(imm32.ImmGetOpenStatus(ctx))
    finally:
        imm32.ImmReleaseContext(hwnd, ctx)


def _vk_of(name: str) -> int | None:
    key = name.strip().lower()
    if key in _VK:
        return _VK[key]
    if len(key) == 1:
        return ord(key.upper())
    return None


def parse_chord(keys: str) -> tuple[list[int], int]:
    """`"ctrl+shift+s"` → ([ctrl, shift], s)；未知键名抛 ValueError。"""
    parts = [p for p in str(keys or "").replace(" ", "").split("+") if p]
    if not parts:
        raise ValueError("keys 为空")
    modifiers: list[int] = []
    final: list[int] = []
    for part in parts:
        vk = _vk_of(part)
        if vk is None:
            raise ValueError(f"无法识别的键名: {part}")
        (modifiers if part.strip().lower() in _MODIFIERS else final).append(vk)
    if len(final) != 1:
        raise ValueError(f"组合键必须且只能有一个主键: {keys}")
    return modifiers, final[0]


def press_chord(keys: str) -> dict:
    modifiers, vk = parse_chord(keys)

    state = ime_open()
    if state is True:
        return {
            "ok": False,
            "error": "ime_intercept",
            "detail": "输入法处于打开状态，本机 VK 按键会被吞成 229（§8.6 未解决项）",
            "hint": "改用鼠标点击菜单，或用 type 输入文本；不要重复重试同一个键",
        }

    items: list[w.INPUT] = []
    for mod in modifiers:
        items.append(w.key_input(mod))
    extended = w.KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED_VKS else 0
    items.append(w.key_input(vk, 0, extended))
    items.append(w.key_input(vk, 0, extended | w.KEYEVENTF_KEYUP))
    for mod in reversed(modifiers):
        items.append(w.key_input(mod, 0, w.KEYEVENTF_KEYUP))
    res = w.send_input(*items)
    time.sleep(0.03)
    return {"ok": res["accepted"] == res["sent"], "keys": keys, "accepted": res["accepted"]}


def scroll_at(x: int, y: int, *, direction: str, amount: int) -> dict:
    notches = max(1, min(10, int(amount)))
    move_to(x, y)
    if direction in ("up", "down"):
        flag = w.MOUSEEVENTF_WHEEL
        data = 120 * notches * (1 if direction == "up" else -1)
    else:
        flag = w.MOUSEEVENTF_HWHEEL
        data = 120 * notches * (1 if direction == "right" else -1)
    res = w.send_input(w.mouse_input(flag, 0, 0, data))
    return {"notches": notches, "at": [x, y], "accepted": res["accepted"]}
