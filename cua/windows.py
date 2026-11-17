"""窗口枚举 / 聚焦 / 会话状态（纯视觉在桌面上的"我在哪、能操作谁"）。

这三个能力 Anthropic 不需要（它的沙箱只有一个容器），但我们在真实桌面上必须
先知道焦点在哪、屏幕是否可操作，否则后面的坐标动作全是盲点。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import time

from cua import _win32 as w
from cua import dpi

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
DESKTOP_READOBJECTS = 0x0001
DESKTOP_SWITCHDESKTOP = 0x0100
WTS_CURRENT_SESSION = 0xFFFFFFFF
WTS_CONNECT_STATE = 8

_wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)

_ENUM_PROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

_CONNECT_STATE = {
    0: "Active",
    1: "Connected",
    2: "ConnectQuery",
    3: "Shadow",
    4: "Disconnected",
    5: "Idle",
    6: "Listen",
    7: "Reset",
    8: "Down",
}


def proc_name(pid: int) -> str:
    handle = w.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if w.kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        w.kernel32.CloseHandle(handle)


def window_rect(hwnd: int) -> list[int] | None:
    rect = w.RECT()
    if not w.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return [rect.left, rect.top, rect.right, rect.bottom]


def window_title(hwnd: int) -> str:
    length = w.user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    w.user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def window_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    w.user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def list_windows(limit: int = 60) -> dict:
    """枚举可见的顶层窗口；焦点窗口排最前，其余按面积从大到小。"""
    fg = int(w.user32.GetForegroundWindow() or 0)
    rows: list[dict] = []

    def _cb(hwnd, _lparam):
        h = int(hwnd)
        if not w.user32.IsWindowVisible(h):
            return True
        pid = wt.DWORD(0)
        w.user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        if not pid.value:
            return True
        rect = window_rect(h)
        if not rect:
            return True
        title = window_title(h)
        rows.append(
            {
                "hwnd": h,
                "pid": int(pid.value),
                "proc": proc_name(int(pid.value)).split("\\")[-1],
                "title": title[:120],
                "class": window_class(h),
                "rect": rect,
                "focused": h == fg,
            }
        )
        return True

    w.user32.EnumWindows(_ENUM_PROC(_cb), 0)
    rows.sort(
        key=lambda r: (
            0 if r["focused"] else 1,
            -max(0, r["rect"][2] - r["rect"][0]) * max(0, r["rect"][3] - r["rect"][1]),
        )
    )
    return {
        "ok": True,
        "count": len(rows),
        "focused_hwnd": fg,
        "windows": rows[: max(1, limit)],
        "truncated": len(rows) > limit,
    }


def focus_window(hwnd: int) -> dict:
    """把窗口带到前台并恢复（如果最小化）；返回窗口矩形供坐标参考。"""
    h = int(hwnd)
    if not w.user32.IsWindow(h):
        return {"ok": False, "error": "invalid_hwnd", "message": f"窗口不存在: {hwnd}"}
    if w.user32.IsIconic(h):
        w.user32.ShowWindow(h, 9)  # SW_RESTORE
    w.user32.SetForegroundWindow(h)
    time.sleep(0.15)
    fg = int(w.user32.GetForegroundWindow() or 0)
    rect = window_rect(h)
    if fg != h:
        return {
            "ok": False,
            "error": "focus_failed",
            "message": "SetForegroundWindow 未生效（Windows 前台锁）",
            "hint": "先点击窗口可见区域，或改用 list_windows 确认焦点",
            "hwnd": h,
            "rect": rect,
        }
    return {"ok": True, "hwnd": h, "rect": rect, "title": window_title(h)}


def _connect_state() -> int | None:
    pp = ctypes.c_void_p()
    size = wt.DWORD(0)
    try:
        ok = _wtsapi32.WTSQuerySessionInformationW(
            None, WTS_CURRENT_SESSION, WTS_CONNECT_STATE, ctypes.byref(pp), ctypes.byref(size)
        )
        if not ok or size.value != 4:
            return None
        return int(ctypes.cast(pp, ctypes.POINTER(wt.DWORD)).contents.value)
    finally:
        if pp:
            _wtsapi32.WTSFreeMemory(pp)


def session_state() -> dict:
    """锁屏/UAC 安全桌面检测：不可输入时让模型停手，而不是盲点。

    判据（`cua-desktop-bench/privilege_session.py` 实测可用）：
    - `OpenInputDesktop` 拿不到句柄：典型是锁屏或 UAC 安全桌面；
    - 前台窗口类名是 `LogonUI` / `LockApp`：锁屏；
    - WTSConnectState 不是 Active。
    """
    hdesk = w.user32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS | DESKTOP_SWITCHDESKTOP)
    desktop_name = ""
    input_ok = bool(hdesk)
    if hdesk:
        buf = ctypes.create_unicode_buffer(256)
        need = wt.DWORD(0)
        if w.user32.GetUserObjectInformationW(hdesk, 2, buf, 512, ctypes.byref(need)):
            desktop_name = buf.value
        w.user32.CloseDesktop(hdesk)

    fg = int(w.user32.GetForegroundWindow() or 0)
    fg_class = window_class(fg) if fg else ""
    state = _connect_state()
    locked = (
        not input_ok
        or fg_class in ("LogonUI", "LockApp")
        or desktop_name.lower() in ("winlogon", "securedesktop")
        or (state is not None and state != 0)
    )
    return {
        "ok": True,
        "locked": locked,
        "connect_state": _CONNECT_STATE.get(state, state) if state is not None else None,
        "input_desktop_ok": input_ok,
        "input_desktop_name": desktop_name,
        "foreground": {"hwnd": fg, "class": fg_class, "title": window_title(fg) if fg else ""},
        "dpi": dpi.ensure_dpi_awareness(),
    }
