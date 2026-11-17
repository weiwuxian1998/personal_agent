"""Windows 进程存活判定（gateway 巡检用；不引入 tools/*，避免把 langchain 拖进常驻进程）。"""

from __future__ import annotations

import ctypes
from ctypes import wintypes as wt

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    try:
        code = wt.DWORD()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)
