"""Win32 低层绑定（ctypes）：只放类型与最基础的调用，不含业务语义。

桌面输入的实现路径与 `cua-desktop-bench/input_inject.py` 一致（本机实测通过），
这里只做工程化封装：坐标归一化、句柄 restype、错误码回传。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

try:  # shcore 只用于 GetDpiForMonitor（诊断），缺失不影响主路径
    shcore = ctypes.WinDLL("shcore", use_last_error=True)
except OSError:  # pragma: no cover - 老系统
    shcore = None  # type: ignore[assignment]

HANDLE = ctypes.c_void_p
ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


# --------------------------------------------------------------------------- 输入结构
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x01000
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_EXTENDEDKEY = 0x0001


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wt.LONG),
        ("dy", wt.LONG),
        ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wt.WORD),
        ("wScan", wt.WORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", INPUT_UNION)]


def mouse_input(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> INPUT:
    inp = INPUT()
    inp.type = 0
    inp.u.mi = MOUSEINPUT(dx, dy, data, flags, 0, 0)
    return inp


def key_input(vk: int = 0, scan: int = 0, flags: int = 0) -> INPUT:
    inp = INPUT()
    inp.type = 1
    inp.u.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
    return inp


def send_input(*items: INPUT) -> dict[str, int]:
    """一次 SendInput 批量提交；返回 accepted（是否全部被系统接受）与错误码。"""
    if not items:
        return {"sent": 0, "accepted": 0, "err": 0}
    arr = (INPUT * len(items))(*items)
    ctypes.set_last_error(0)
    accepted = user32.SendInput(len(arr), arr, ctypes.sizeof(INPUT))
    return {"sent": len(arr), "accepted": int(accepted), "err": ctypes.get_last_error()}


# --------------------------------------------------------------------------- 屏幕指标
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
SM_CXSCREEN = 0
SM_CYSCREEN = 1


def primary_size() -> tuple[int, int]:
    """主屏物理像素尺寸（per-monitor V2 下即真实分辨率）。"""
    return int(user32.GetSystemMetrics(SM_CXSCREEN)), int(
        user32.GetSystemMetrics(SM_CYSCREEN)
    )


def virtual_desktop() -> tuple[int, int, int, int]:
    """虚拟桌面 (x, y, cx, cy)；单屏时原点 (0,0)、尺寸等于主屏。"""
    return (
        int(user32.GetSystemMetrics(SM_XVIRTUALSCREEN)),
        int(user32.GetSystemMetrics(SM_YVIRTUALSCREEN)),
        int(user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)),
        int(user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)),
    )


# --------------------------------------------------------------------------- GDI 截图
class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wt.WORD),
        ("biBitCount", wt.WORD),
        ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


user32.GetDC.restype = HANDLE
user32.GetDC.argtypes = [HANDLE]
user32.ReleaseDC.argtypes = [HANDLE, HANDLE]
gdi32.CreateCompatibleDC.restype = HANDLE
gdi32.CreateCompatibleDC.argtypes = [HANDLE]
gdi32.CreateCompatibleBitmap.restype = HANDLE
gdi32.CreateCompatibleBitmap.argtypes = [HANDLE, ctypes.c_int, ctypes.c_int]
gdi32.SelectObject.restype = HANDLE
gdi32.SelectObject.argtypes = [HANDLE, HANDLE]
gdi32.BitBlt.restype = ctypes.c_int
gdi32.BitBlt.argtypes = [
    HANDLE, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    HANDLE, ctypes.c_int, ctypes.c_int, wt.DWORD,
]
gdi32.GetDIBits.restype = ctypes.c_int
gdi32.GetDIBits.argtypes = [
    HANDLE, HANDLE, wt.UINT, wt.UINT, ctypes.c_void_p,
    ctypes.POINTER(BITMAPINFOHEADER), wt.UINT,
]
gdi32.DeleteObject.argtypes = [HANDLE]
gdi32.DeleteDC.argtypes = [HANDLE]

# --------------------------------------------------------------------------- 常用签名
# 64 位下 HWND/HANDLE 默认按 c_int 截断，句柄一旦被截断，窗口函数会"静默失败"。
user32.GetForegroundWindow.restype = HANDLE
user32.GetWindowTextLengthW.argtypes = [HANDLE]
user32.GetWindowTextW.argtypes = [HANDLE, wt.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [HANDLE, wt.LPWSTR, ctypes.c_int]
user32.IsWindowVisible.argtypes = [HANDLE]
user32.IsWindow.argtypes = [HANDLE]
user32.IsIconic.argtypes = [HANDLE]
user32.ShowWindow.argtypes = [HANDLE, ctypes.c_int]
user32.SetForegroundWindow.argtypes = [HANDLE]
user32.GetWindowRect.argtypes = [HANDLE, ctypes.POINTER(RECT)]
user32.GetWindowThreadProcessId.argtypes = [HANDLE, ctypes.POINTER(wt.DWORD)]
user32.OpenInputDesktop.restype = HANDLE
user32.OpenInputDesktop.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
user32.CloseDesktop.argtypes = [HANDLE]
user32.GetUserObjectInformationW.argtypes = [
    HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD)
]
user32.GetSystemMetrics.restype = ctypes.c_int

kernel32.OpenProcess.restype = HANDLE
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.QueryFullProcessImageNameW.argtypes = [
    HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)
]
kernel32.CloseHandle.argtypes = [HANDLE]


def grab_bgra(x: int, y: int, width: int, height: int) -> bytes:
    """从屏幕 DC 抓一块矩形，返回 top-down 的 BGRA 原始字节。"""
    hdc_screen = user32.GetDC(None)
    if not hdc_screen:
        raise OSError("GetDC 失败")
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    hbmp = gdi32.CreateCompatibleBitmap(hdc_screen, width, height)
    old = gdi32.SelectObject(hdc_mem, hbmp)
    try:
        ok = gdi32.BitBlt(hdc_mem, 0, 0, width, height, hdc_screen, x, y, 0x00CC0020)
        if not ok:
            raise OSError(f"BitBlt 失败 err={ctypes.get_last_error()}")
        header = BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        header.biWidth = width
        header.biHeight = -height  # 负值 = top-down
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = 0
        buf = ctypes.create_string_buffer(width * height * 4)
        lines = gdi32.GetDIBits(
            hdc_mem, hbmp, 0, height, buf, ctypes.byref(header), 0
        )
        if lines != height:
            raise OSError(f"GetDIBits 只返回 {lines}/{height} 行")
        return buf.raw
    finally:
        gdi32.SelectObject(hdc_mem, old)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(None, hdc_screen)
