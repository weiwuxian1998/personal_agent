"""Shell 后端：四段探测、argv 构建、最小环境与输出去编码。

设计依据：契约 §7.2 第 1/2/7 条、设计文档 §7.0/§7.3.1/§7.3.3。
只依赖标准库，不 import config/contracts。
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# 契约 §7.2 第 2 条规定的脚本前缀与结尾（原文，不要改）。
UTF8_PREFIX = "try{[Console]::OutputEncoding=[Text.Encoding]::UTF8}catch{};"
UTF8_INPUT = "$OutputEncoding=[Text.Encoding]::UTF8;$env:PYTHONUTF8='1';$env:PYTHONIOENCODING='utf-8';"
PROGRESS_PREFIX = "$ProgressPreference='SilentlyContinue';"
# 末尾仍是契约要求的 `;exit $LASTEXITCODE`；前面的守卫把 cmdlet 的
# 非终止错误（$? = false 但 $LASTEXITCODE 未设置）归一化为退出码 1，
# 同时不破坏原生程序的 $LASTEXITCODE 透传。
EXIT_TAIL = (
    ";if(-not $?){if($LASTEXITCODE){exit $LASTEXITCODE}else{exit 1}}"
    ";exit $LASTEXITCODE"
)

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 只按白名单注入：绝不把 .env 里的密钥/DSN 透传给子进程。
_ENV_WHITELIST = frozenset(
    {
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "PATH",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "COMMONPROGRAMFILES",
        "COMMONPROGRAMFILES(X86)",
        "PUBLIC",
        "ALLUSERSPROFILE",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
        "PROCESSOR_IDENTIFIER",
        "OS",
        "USERNAME",
        "USERDOMAIN",
        "COMPUTERNAME",
        "LOGONSERVER",
        "SESSIONNAME",
        "PSMODULEPATH",
    }
)


@dataclass(frozen=True, slots=True)
class ShellInfo:
    """探测结果；`name` ∈ pwsh / powershell / cmd（wsl 由显式参数构造）。"""

    name: str
    exe: str
    version: str


# ----------------------------------------------------------------------------- 环境
def minimal_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """白名单环境：保留系统必需项，去掉一切凭据/业务配置。"""
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in _ENV_WHITELIST
    }
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    if extra:
        env.update({key: str(value) for key, value in extra.items()})
    return env


# ----------------------------------------------------------------------------- 探测
def _windowsapps_pwsh() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Microsoft" / "WindowsApps" / "pwsh.exe"


def _system32() -> Path:
    return Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"


def _candidates() -> list[tuple[str, Path]]:
    """契约 §7.0 的探测顺序；同路径去重，避免重复起进程取版本。"""
    items: list[tuple[str, Path]] = []
    seen: set[str] = set()

    def add(name: str, path: str | os.PathLike[str] | None) -> None:
        if not path:
            return
        candidate = Path(path)
        key = str(candidate).lower()
        if key in seen:
            return
        seen.add(key)
        items.append((name, candidate))

    add("pwsh", shutil.which("pwsh"))
    add("pwsh", _windowsapps_pwsh())
    add("pwsh", Path(r"C:\Program Files\PowerShell\7\pwsh.exe"))
    add("powershell", shutil.which("powershell"))
    add(
        "powershell",
        _system32() / "WindowsPowerShell" / "v1.0" / "powershell.exe",
    )
    add("cmd", os.environ.get("COMSPEC"))
    add("cmd", _system32() / "cmd.exe")
    return items


def _probe(name: str, exe: Path) -> ShellInfo | None:
    try:
        if not exe.is_file():
            return None
    except OSError:
        return None

    if name == "cmd":
        argv = [str(exe), "/d", "/c", "ver"]
    else:
        argv = [
            str(exe),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$PSVersionTable.PSVersion.ToString()",
        ]
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            timeout=20,
            env=minimal_env(),
            creationflags=_CREATE_NO_WINDOW,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    text = decode_bytes(completed.stdout, name).strip()
    if name == "cmd":
        match = re.search(r"(\d+\.\d+\.\d+\.\d+)", text)
        version = match.group(1) if match else platform.version()
    else:
        match = re.search(r"(\d+\.\d+(?:\.\d+)*)", text)
        if not match:
            return None
        version = match.group(1)
    return ShellInfo(name=name, exe=str(exe), version=version)


@lru_cache(maxsize=1)
def detect_shell() -> ShellInfo:
    """四段探测（PATH → WindowsApps alias → Program Files → 5.1 → cmd）。

    进程内只算一次并缓存；`Shell(shell="auto")` 不会每次重新探测。
    """
    for name, exe in _candidates():
        info = _probe(name, exe)
        if info is not None:
            return info
    raise RuntimeError("未找到可用的 shell（pwsh / powershell / cmd 均不可用）")


def reset_shell_cache() -> None:
    """仅供验收脚本/测试使用；业务代码不要调用。"""
    detect_shell.cache_clear()


def _wsl_version(exe: str) -> str:
    try:
        completed = subprocess.run(
            [exe, "--version"],
            capture_output=True,
            timeout=20,
            env=minimal_env(),
            creationflags=_CREATE_NO_WINDOW,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    text = decode_bytes(completed.stdout, "wsl")
    match = re.search(r"(\d+\.\d+\.\d+(?:\.\d+)?)", text)
    return match.group(1) if match else ""


def find_shell(name: str = "auto") -> ShellInfo | None:
    """按名称取 shell；找不到返回 None（auto 无可回退时抛 RuntimeError）。"""
    name = (name or "auto").lower()
    if name == "auto":
        return detect_shell()
    if name == "wsl":
        exe = shutil.which("wsl") or str(_system32() / "wsl.exe")
        return ShellInfo("wsl", exe, _wsl_version(exe)) if Path(exe).is_file() else None
    if name not in ("pwsh", "powershell", "cmd"):
        raise ValueError(f"不支持的 shell: {name}")
    for candidate_name, exe in _candidates():
        if candidate_name == name:
            info = _probe(candidate_name, exe)
            if info is not None:
                return info
    return None


# ----------------------------------------------------------------------------- argv
def padded_script(script: str) -> str:
    """PowerShell 完整脚本：UTF-8 前缀 + 用户脚本 + exit $LASTEXITCODE。"""
    return UTF8_PREFIX + UTF8_INPUT + PROGRESS_PREFIX + script + EXIT_TAIL


def build_command(shell: ShellInfo, script: str) -> list[str]:
    """argv 单层：整段脚本作为一个参数，shell=False 直接交给 CreateProcess。"""
    if shell.name in ("pwsh", "powershell"):
        return [
            shell.exe,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            padded_script(script),
        ]
    if shell.name == "cmd":
        return [shell.exe, "/d", "/c", "chcp 65001>nul & " + script]
    if shell.name == "wsl":
        return [shell.exe, "-e", "bash", "-lc", script]
    raise ValueError(f"不支持的 shell: {shell.name}")


def build_file_command(shell: ShellInfo, script_path: Path) -> list[str]:
    """超长脚本兜底：写临时 .ps1（UTF-8 BOM）后 `-File` 执行。"""
    if shell.name not in ("pwsh", "powershell"):
        raise ValueError(f"{shell.name} 不支持临时脚本兜底")
    return [
        shell.exe,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
    ]


def argv_chars(argv: list[str]) -> int:
    """估算 CreateProcess 命令行长度（含分隔空格）。"""
    return sum(len(part) + 1 for part in argv)


# ----------------------------------------------------------------------------- 解码
def decode_bytes(raw: bytes, source: str) -> str:
    """按来源解码：WSL 偶发 UTF-16LE（`w s l :` 隔字符乱码），其余按 UTF-8。"""
    if not raw:
        return ""
    if source == "wsl" and len(raw) % 2 == 0:
        sample = raw[:4096]
        if sample and sample.count(b"\x00") / len(sample) > 0.2:
            return raw.decode("utf-16-le", errors="replace")
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    return raw.decode("utf-8", errors="replace")
