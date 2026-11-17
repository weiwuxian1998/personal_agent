"""浏览器内核启动（单点配置，**不是模型工具**）。

§8.2 明确不提供 `open_app`：启动程序走开始菜单的视觉操作。但"用哪个 Chromium"
是环境准备，属于运行时职责，只能有一处配置——`Settings.cua_browser_exe`：

- 默认：`D:\\repo\\chrome-win64\\chrome.exe`（145）
- 备选：`...\\cua-browsers\\chromium-1254\\chrome-win64\\chrome.exe`（158）

切换内核只改这一项配置，不改代码。用独立 user-data-dir，避免和用户日常 Chrome
抢单实例锁，也让首次启动行为可复现。
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from config import Settings
from cua import windows

logger = logging.getLogger("cua.browser")


def browser_exe(settings: Settings) -> Path:
    return Path(settings.cua_browser_exe).expanduser()


def _running_pid(exe: Path) -> int | None:
    """找到已在运行的同一可执行文件（按完整镜像路径比对）。"""
    target = str(exe.resolve()).lower()
    listed = windows.list_windows(limit=200)
    seen: set[int] = set()
    for row in listed.get("windows", []):
        pid = int(row["pid"])
        if pid in seen:
            continue
        seen.add(pid)
        full = windows.proc_name(pid)
        if full and full.lower() == target:
            return pid
    return None


def ensure_browser(settings: Settings) -> dict:
    """按配置启动浏览器内核；已在运行就复用（不重复开窗口）。"""
    exe = browser_exe(settings)
    if not exe.exists():
        logger.warning("CUA 浏览器内核不存在: %s", exe)
        return {
            "ok": False,
            "error": "browser_missing",
            "message": f"chrome exe 不存在: {exe}",
            "exe": str(exe),
        }

    pid = _running_pid(exe)
    if pid is not None:
        return {"ok": True, "launched": False, "pid": pid, "exe": str(exe)}

    profile = Path(settings.data_dir) / "cua" / "chrome-profile"
    profile.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(exe),
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        "--window-position=60,60",
        "--window-size=1280,800",
        "about:blank",
    ]
    try:
        proc = subprocess.Popen(cmd, close_fds=True)
    except OSError as exc:
        return {"ok": False, "error": "browser_spawn_failed", "message": str(exc), "exe": str(exe)}
    logger.info("已启动 CUA 浏览器内核 pid=%s exe=%s", proc.pid, exe)
    return {
        "ok": True,
        "launched": True,
        "pid": proc.pid,
        "exe": str(exe),
        "profile": str(profile),
        "proc": proc,
    }


def stop_browser(state: dict | None) -> None:
    """只关闭**本进程启动的**实例；复用的已有实例不动。"""
    if not state or not state.get("launched"):
        return
    proc = state.get("proc")
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=3)
    except Exception:  # noqa: BLE001 - 关不掉就强杀，不影响任务结论
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            logger.warning("关闭 CUA 浏览器失败（忽略）", exc_info=True)
