"""Shell 工具（M-SHELL）：argv 单层传参、UTF-8 前缀、taskkill + Job Object 双保险、超长输出落盘。

设计依据：契约 §7.2、设计文档 §7.3/§7.3.1/§7.3.4/§7.5.1。
本模块对外只暴露契约规定的工厂 `build_shell_tools`，以及 M-WORKER 复用的
`stub_text` 与后台命令占位接口。
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, tool

from config import settings
from contracts import ToolProfile, ulid
from tools import winjob
from tools.errors import (
    HINT_NOT_FOUND_PATH,
    ErrorInfo,
    RetryTracker,
    classify_error,
    clean_text,
    fingerprint,
    normalize_command_for_fingerprint,
    summarize_error,
)
from tools.shell_backend import (
    ShellInfo,
    argv_chars,
    build_command,
    build_file_command,
    decode_bytes,
    find_shell,
    minimal_env,
    padded_script,
)

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_MAX_ROUTER_TIMEOUT_MS = 30000
_MAX_WORKER_TIMEOUT_MS = 600000
_DEFAULT_WORKER_TIMEOUT_MS = 120000
# Windows CreateProcess 命令行上限约 32767 字符，留余量给环境与参数。
_MAX_ARGV_CHARS = 28000

_DEFAULT_STUB_HINT = (
    "[hint] 输出未截断到不可用：完整内容在上面的绝对路径。\n"
    "用 Grep 按关键字定位（如 `error|failed|Exception`），\n"
    "或 Read 该文件并带 offset/limit 分段读取。"
)

_RETRY = RetryTracker()

_BG_LOCK = threading.Lock()
_BACKGROUND: dict[str, tuple[subprocess.Popen, int | None]] = {}


def reset_retry_state() -> None:
    """仅供验收脚本/新轮次重置进程内计数；不改变执行语义。"""
    _RETRY.reset()


# ----------------------------------------------------------------------------- 输出统计 / stub
def format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size}B"
    value = float(size)
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1024
        if value < 1024 or unit == "TB":
            return f"{value:.1f}{unit}"
    return f"{size}B"


def count_lines(text: str) -> int:
    return len(text.splitlines())


def needs_artifact(text: str, *, max_lines: int, max_bytes: int) -> bool:
    """契约 §7.3.4：行数 > 2000 或字节 > 64 KB（任一命中）。"""
    return count_lines(text) > max_lines or len(text.encode("utf-8")) > max_bytes


def stub_text(
    text: str,
    artifact: str,
    *,
    head_lines: int = 30,
    tail_lines: int = 60,
    header: str = "",
    hint: str = "",
) -> str:
    """超长输出替身：头 N + 尾 M + 统计由 header 提供 + 绝对路径 + 读取提示。

    WebFetch/read_session 等工具复用同一段格式化逻辑（契约 §7.5）。
    """
    lines = text.splitlines()
    total = len(lines)
    head = lines[:head_lines]
    tail_start = max(head_lines, total - tail_lines)
    tail = lines[tail_start:]

    parts: list[str] = []
    if header:
        parts.append(header)
    parts.append(f"[artifact] {artifact}")
    parts.append("")
    parts.append(f"--- head (1-{len(head)}) ---")
    parts.extend(head)
    parts.append("")
    parts.append(f"--- tail (last {len(tail)}) ---")
    parts.extend(tail)
    parts.append("")
    parts.append(hint or _DEFAULT_STUB_HINT)
    return "\n".join(parts)


# ----------------------------------------------------------------------------- 后台命令占位
def register_background_command(
    command_id: str,
    proc: subprocess.Popen,
    job: int | None = None,
) -> None:
    """登记本进程内启动的后台命令（v1 Shell 同步执行，不会调用）。"""
    with _BG_LOCK:
        _BACKGROUND[command_id] = (proc, job)


def stop_background_command(command_id: str) -> bool:
    """停止已登记的后台命令；未登记返回 False（供 worker StopCommand 使用）。"""
    with _BG_LOCK:
        entry = _BACKGROUND.pop(command_id, None)
    if entry is None:
        return False
    proc, job = entry
    _kill_tree(job, proc)
    if job:
        try:
            winjob.close_job(job)
        except OSError:
            pass
    return True


# ----------------------------------------------------------------------------- 进程执行
def _kill_tree(job: int | None, proc: subprocess.Popen) -> None:
    """杀整棵树：先 taskkill /T 覆盖 breakaway 子进程，再 TerminateJobObject。

    某些宿主（沙箱/嵌套 Job）会把 PowerShell 派生的原生进程移出 Job，单独
    TerminateJobObject 会漏杀孙进程；taskkill /T 按当时仍存在的父子关系兜底。
    """
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            shell=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=minimal_env(),
            creationflags=_CREATE_NO_WINDOW,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass
    if job:
        try:
            winjob.terminate_job(job)
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _spawn_and_wait(
    argv: list[str],
    *,
    cwd: Path,
    timeout_ms: int,
) -> tuple[bytes, bytes, int | None, bool]:
    """返回 (raw_stdout, raw_stderr, exit_code, timed_out)。"""
    job: int | None = None
    try:
        job = winjob.create_kill_on_close_job()
    except OSError:
        job = None

    try:
        proc = subprocess.Popen(
            argv,
            shell=False,
            cwd=str(cwd),
            env=minimal_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=_CREATE_NO_WINDOW,
        )
    except BaseException:
        if job:
            try:
                winjob.close_job(job)
            except OSError:
                pass
        raise

    if job:
        try:
            winjob.assign_process(job, proc)
        except OSError:
            pass

    timed_out = False
    try:
        try:
            raw_out, raw_err = proc.communicate(timeout=timeout_ms / 1000)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(job, proc)
            try:
                raw_out, raw_err = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass
                raw_out, raw_err = proc.communicate()
    finally:
        if job:
            try:
                winjob.close_job(job)
            except OSError:
                pass

    return raw_out, raw_err, (None if timed_out else proc.returncode), timed_out


# ----------------------------------------------------------------------------- 返回体
def _combine(stdout: str, stderr: str) -> str:
    if not stderr:
        return stdout
    if not stdout:
        return "--- stderr ---\n" + stderr
    separator = "" if stdout.endswith("\n") else "\n"
    return stdout + separator + "--- stderr ---\n" + stderr


def _base_body(shell_name: str) -> dict[str, Any]:
    return {
        "ok": False,
        "exit_code": None,
        "stdout": "",
        "stderr": "",
        "total_lines": 0,
        "total_bytes": 0,
        "artifact": None,
        "duration_ms": 0,
        "shell": shell_name,
        "truncated": False,
    }


def _early_failure(
    *,
    command: str,
    shell_name: str,
    error_class: str,
    message: str,
    hint: str,
    hint_key: str | None = None,
    retryable: bool = False,
    started: float,
) -> dict[str, Any]:
    """参数/环境校验失败（命令未执行）：不更新进程内计数。"""
    fp = fingerprint(shell_name or "unknown", command, error_class, hint_key or error_class)
    body = _base_body(shell_name)
    body.update(
        {
            "ok": False,
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "error_class": error_class,
            "fingerprint": fp,
            "hint": hint,
            "retryable": retryable,
            "attempt": 1,
            "same_fp_count": 1,
            "message": message,
        }
    )
    return body


def _write_artifact(directory: Path, text: str) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    path = (directory / f"sh_{ulid()}.txt").resolve()
    path.write_text(text, encoding="utf-8", newline="\n")
    return str(path)


# ----------------------------------------------------------------------------- 主流程
def _execute(
    *,
    command: str,
    cwd: str | None,
    timeout_ms: int | None,
    shell: str,
    profile: ToolProfile,
    default_cwd: Path,
    max_timeout_ms: int,
    default_timeout_ms: int,
    tool_outputs_dir: Path,
) -> dict[str, Any]:
    started = time.perf_counter()

    if not isinstance(command, str) or not command.strip():
        return _early_failure(
            command=command if isinstance(command, str) else "",
            shell_name=shell or "auto",
            error_class="parse_error",
            message="command 不能为空",
            hint="传入完整脚本（可含换行）作为单个字符串；多条语句用 ';' 串联",
            started=started,
        )

    if timeout_ms is None:
        timeout_ms = default_timeout_ms
    try:
        timeout_ms = int(timeout_ms)
    except (TypeError, ValueError):
        return _early_failure(
            command=command,
            shell_name=shell or "auto",
            error_class="parse_error",
            message="timeout_ms 必须是整数（毫秒）",
            hint=f"worker 默认 {default_timeout_ms}，上限 {max_timeout_ms}",
            started=started,
        )

    if timeout_ms <= 0:
        return _early_failure(
            command=command,
            shell_name=shell or "auto",
            error_class="parse_error",
            message="timeout_ms 必须大于 0",
            hint=f"worker 默认 {default_timeout_ms}，上限 {max_timeout_ms}",
            started=started,
        )

    if timeout_ms > max_timeout_ms:
        if profile == "router":
            return _early_failure(
                command=command,
                shell_name=shell or "auto",
                error_class="router_long_command",
                message=f"router 侧 Shell 超时上限 {_MAX_ROUTER_TIMEOUT_MS} ms，收到 {timeout_ms} ms",
                hint="router 不跑长命令：改用 create_session 派 worker，然后本轮结束",
                hint_key="router_long_command",
                started=started,
            )
        return _early_failure(
            command=command,
            shell_name=shell or "auto",
            error_class="parse_error",
            message=f"worker 侧 Shell 超时上限 {_MAX_WORKER_TIMEOUT_MS} ms，收到 {timeout_ms} ms",
            hint=f"拆成更小的步骤；单次 timeout_ms 不得超过 {_MAX_WORKER_TIMEOUT_MS}",
            started=started,
        )

    requested_shell = (shell or "auto").lower()
    try:
        info = find_shell(requested_shell)
    except ValueError:
        return _early_failure(
            command=command,
            shell_name=requested_shell,
            error_class="command_not_found",
            message=f"不支持的 shell: {requested_shell}",
            hint="shell 只能是 auto / pwsh / powershell / cmd / wsl",
            started=started,
        )
    except RuntimeError as exc:
        return _early_failure(
            command=command,
            shell_name=requested_shell,
            error_class="command_not_found",
            message=str(exc),
            hint="确认已安装 PowerShell 7（或 5.1），或显式传 shell='cmd'",
            started=started,
        )
    if info is None:
        return _early_failure(
            command=command,
            shell_name=requested_shell,
            error_class="command_not_found",
            message=f"未找到可用的 shell: {requested_shell}",
            hint="确认已安装 PowerShell 7（或 5.1），或显式传 shell='cmd'",
            started=started,
        )

    base_cwd = Path(default_cwd).expanduser()
    candidate = Path(cwd).expanduser() if cwd else base_cwd
    if not candidate.is_absolute():
        candidate = base_cwd / candidate
    try:
        run_cwd = candidate.resolve()
    except OSError:
        run_cwd = candidate.absolute()
    if not run_cwd.is_dir():
        return _early_failure(
            command=command,
            shell_name=info.name,
            error_class="not_found_path",
            message=f"cwd 不存在或不是目录: {run_cwd}",
            hint=HINT_NOT_FOUND_PATH,
            started=started,
        )

    temp_path: Path | None = None
    argv: list[str]
    try:
        argv = build_command(info, command)
        if argv_chars(argv) > _MAX_ARGV_CHARS:
            if info.name in ("pwsh", "powershell"):
                fd, name = tempfile.mkstemp(prefix="asst_shell_", suffix=".ps1")
                os.close(fd)
                temp_path = Path(name)
                temp_path.write_text(
                    padded_script(command), encoding="utf-8-sig", newline="\r\n"
                )
                argv = build_file_command(info, temp_path)
            else:
                return _early_failure(
                    command=command,
                    shell_name=info.name,
                    error_class="parse_error",
                    message=f"命令过长（{len(command)} 字符），{info.name} 不支持超长脚本兜底",
                    hint="把脚本写入文件，再用解释器/脚本路径执行",
                    started=started,
                )
    except OSError as exc:
        return _early_failure(
            command=command,
            shell_name=info.name,
            error_class="permission",
            message=f"准备执行失败：{exc}",
            hint="检查临时目录/工作目录权限",
            started=started,
        )

    try:
        try:
            raw_out, raw_err, exit_code, timed_out = _spawn_and_wait(
                argv, cwd=run_cwd, timeout_ms=timeout_ms
            )
        except FileNotFoundError as exc:
            return _early_failure(
                command=command,
                shell_name=info.name,
                error_class="command_not_found",
                message=f"无法启动 shell：{exc}",
                hint="确认 shell 路径存在；Get-Command pwsh",
                started=started,
            )
        except PermissionError as exc:
            return _early_failure(
                command=command,
                shell_name=info.name,
                error_class="permission",
                message=f"启动 shell 被拒绝：{exc}",
                hint="不要提权；检查执行策略与文件权限",
                started=started,
            )
        except OSError as exc:
            return _early_failure(
                command=command,
                shell_name=info.name,
                error_class="command_not_found",
                message=f"启动 shell 失败：{exc}",
                hint="确认 shell 路径存在；或显式传 shell='cmd'",
                started=started,
            )
        except ValueError as exc:
            return _early_failure(
                command=command,
                shell_name=info.name,
                error_class="parse_error",
                message=f"命令包含无法传给进程的参数：{exc}",
                hint="检查命令里是否含空字符等非法参数",
                started=started,
            )
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass

    duration_ms = int((time.perf_counter() - started) * 1000)
    stdout = clean_text(decode_bytes(raw_out, info.name))
    stderr = clean_text(decode_bytes(raw_err, info.name))
    combined = _combine(stdout, stderr)
    total_lines = count_lines(combined)
    total_bytes = len(combined.encode("utf-8"))
    truncated = needs_artifact(
        combined,
        max_lines=settings.tool_output_max_lines,
        max_bytes=settings.tool_output_max_bytes,
    )

    artifact: str | None = None
    stdout_out, stderr_out = stdout, stderr
    if truncated:
        try:
            artifact = _write_artifact(tool_outputs_dir, combined)
            header = (
                f"[shell] exit={exit_code if exit_code is not None else 'None'}  "
                f"total_lines={total_lines}  total_bytes={format_bytes(total_bytes)}  "
                f"time={duration_ms / 1000:.1f}s"
            )
            stdout_out = stub_text(
                combined,
                artifact=artifact,
                head_lines=settings.tool_output_head_lines,
                tail_lines=settings.tool_output_tail_lines,
                header=header,
            )
            stderr_out = ""
        except OSError as exc:
            # 落盘失败时宁可回完整输出，也不要静默丢数据。
            truncated = False
            artifact = None
            stderr_out = (stderr + f"\n[warning] 超长输出落盘失败: {exc}").strip()

    body = _base_body(info.name)
    body.update(
        {
            "exit_code": exit_code,
            "stdout": stdout_out,
            "stderr": stderr_out,
            "total_lines": total_lines,
            "total_bytes": total_bytes,
            "artifact": artifact,
            "duration_ms": duration_ms,
            "truncated": truncated,
        }
    )

    error = classify_error(
        exit_code,
        combined,
        timed_out=timed_out,
        command=command,
        shell_name=info.name,
    )
    if error is None:
        body["ok"] = True
        _RETRY.record_success(normalize_command_for_fingerprint(command))
        return body

    body["ok"] = False
    fp = fingerprint(info.name, command, error.error_class, error.hint_key)
    same_fp_count, attempt = _RETRY.record_failure(
        normalize_command_for_fingerprint(command), fp
    )
    body.update(
        {
            "error_class": error.error_class,
            "fingerprint": fp,
            "hint": error.hint,
            "retryable": error.retryable,
            "attempt": attempt,
            "same_fp_count": same_fp_count,
            "message": summarize_error(
                error,
                combined,
                exit_code=exit_code,
                timeout_ms=timeout_ms,
            ),
        }
    )
    return body


# ----------------------------------------------------------------------------- 工厂
def build_shell_tools(
    *,
    profile: ToolProfile,
    workspace_root: Path,
    tool_outputs_dir: Path,
    default_cwd: Path,
) -> list[BaseTool]:
    """按 profile 注入超时上限，返回 [Shell]。"""
    if profile not in ("router", "worker"):
        raise ValueError(f"未知 profile: {profile}")

    max_timeout_ms = _MAX_ROUTER_TIMEOUT_MS if profile == "router" else _MAX_WORKER_TIMEOUT_MS
    default_timeout_ms = _MAX_ROUTER_TIMEOUT_MS if profile == "router" else _DEFAULT_WORKER_TIMEOUT_MS

    fallback_cwd = Path(default_cwd).expanduser()
    if not fallback_cwd.is_dir():
        fallback_cwd = Path(workspace_root).expanduser()
    if not fallback_cwd.is_dir():
        fallback_cwd = Path.cwd()
    outputs_dir = Path(tool_outputs_dir).expanduser()

    @tool
    def Shell(
        command: str,
        cwd: str | None = None,
        timeout_ms: int | None = None,
        shell: str = "auto",
    ) -> dict[str, Any]:
        """在 Windows 上执行一条命令（无状态：每次新进程 + 独立 Job，cwd/env 不继承）。

        每次调用会启动一个全新的独立进程，并建立一个独立的 Windows Job Object。
        cwd 与 env 不会从上一次调用继承——上一次的 cd、$env:XXX=...、Set-Location、
        别名/函数定义，下一次全部消失。需要连续状态时：(1) 在同一次调用的脚本里用
        ';' 串起来；(2) 用绝对路径；(3) 需要后台常驻的任务，自己在命令末尾用
        Start-Process 并把 pid 写进产物，不要指望第二次调用还在原进程里。

        边界：
        - shell="auto" 按 pwsh(7) → powershell(5.1) → cmd 顺序探测；
          也可显式传 pwsh / powershell / cmd / wsl。
        - router profile 的 timeout_ms 上限 30000；worker 上限 600000。
        - cwd 不存在直接返回 not_found_path，不执行命令。
        - 超时用 `taskkill /F /T` + `TerminateJobObject` 双保险杀整棵进程树，不会留下后台进程。
        - 输出超过 2000 行或 64 KB（任一）时，完整内容写
          tool_outputs/sh_<ulid>.txt，stdout 只保留头 30 + 尾 60 行 +
          统计 + 绝对路径 + Grep/Read 提示。

        返回：
        {ok, exit_code, stdout, stderr, total_lines, total_bytes, artifact,
         duration_ms, shell, truncated}；失败时追加 error_class / fingerprint /
         hint / retryable / attempt / same_fp_count / message。
        """
        return _execute(
            command=command,
            cwd=cwd,
            timeout_ms=timeout_ms,
            shell=shell,
            profile=profile,
            default_cwd=fallback_cwd,
            max_timeout_ms=max_timeout_ms,
            default_timeout_ms=default_timeout_ms,
            tool_outputs_dir=outputs_dir,
        )

    return [Shell]
