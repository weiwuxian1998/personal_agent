"""DPI 声明：进程启动即 per-monitor V2（§8.3 硬要求）。

实测依据（`docs/CUA_DESKTOP_FEASIBILITY.md` §3）：
- 不声明时，3072×1920 的屏只能截到 1536×960（左上角 1:1 裁剪，丢 75%），
  且所有坐标 API 静默按 50% 折算，**不报任何错**；
- system aware 在多屏下会把 100% 的副屏虚报成 2×。

所以这里**写死一次**，不做事后纠正：模块导入即调用，不做"检测到偏移再补"。
`SetProcessDpiAwarenessContext` 必须在任何窗口/截图/坐标 API 之前执行；重复调用
返回失败（ERROR_ACCESS_DENIED），此时以线程实际 awareness 为准，不覆盖。
"""

from __future__ import annotations

import ctypes
import logging

logger = logging.getLogger("cua.dpi")

DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4

_AWARENESS_NAMES = {0: "UNAWARE", 1: "SYSTEM_AWARE", 2: "PER_MONITOR_AWARE"}

_STATE: dict[str, object] = {
    "applied": False,
    "ok": False,
    "actual": "",
    "error": "",
}


def _query_actual(user32: ctypes.WinDLL) -> str:
    """读取线程当前 awareness（V1/V2 都归为 PER_MONITOR_AWARE，够用）。"""
    try:
        user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        user32.GetThreadDpiAwarenessContext.argtypes = []
        user32.GetAwarenessFromDpiAwarenessContext.restype = ctypes.c_int
        user32.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        ctx = user32.GetThreadDpiAwarenessContext()
        value = user32.GetAwarenessFromDpiAwarenessContext(ctypes.c_void_p(ctx))
        return _AWARENESS_NAMES.get(value, str(value))
    except Exception:  # noqa: BLE001 - 老系统没有这些 API
        return "unknown"


def ensure_dpi_awareness() -> dict[str, object]:
    """声明 per-monitor V2；只声明一次，之后返回首次结果（幂等）。"""
    if _STATE["applied"]:
        return dict(_STATE)

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user32.SetProcessDpiAwarenessContext.restype = ctypes.c_int

    ctypes.set_last_error(0)
    ok = bool(
        user32.SetProcessDpiAwarenessContext(
            ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
        )
    )
    err = ctypes.get_last_error()
    actual = _query_actual(user32)

    _STATE.update(
        {
            "applied": True,
            "ok": ok,
            "actual": actual,
            "error": "" if ok else f"SetProcessDpiAwarenessContext failed err={err}",
        }
    )
    if ok:
        logger.info("DPI awareness = per-monitor V2（已声明）")
    else:
        # 进程可能已被宿主/清单设过 awareness；不报错、不纠正，只如实记录。
        logger.warning(
            "DPI awareness 声明未生效（err=%s），实际为 %s；坐标与截图按实际值解释",
            err,
            actual,
        )
    return dict(_STATE)


ensure_dpi_awareness()
