"""Windows Job Object 封装：创建即 `KILL_ON_JOB_CLOSE`，超时可原子杀整棵树。

设计依据：契约 §7.2 第 3 条、设计文档 §7.3.2。
只依赖标准库（ctypes），不 import 其他模块。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import subprocess

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JobObjectExtendedLimitInformation = 9


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wt.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wt.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wt.DWORD),
        ("SchedulingClass", wt.DWORD),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


if os.name == "nt":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wt.HANDLE

    _kernel32.SetInformationJobObject.argtypes = [
        wt.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wt.DWORD,
    ]
    _kernel32.SetInformationJobObject.restype = wt.BOOL

    _kernel32.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wt.BOOL

    _kernel32.TerminateJobObject.argtypes = [wt.HANDLE, wt.UINT]
    _kernel32.TerminateJobObject.restype = wt.BOOL

    _kernel32.CloseHandle.argtypes = [wt.HANDLE]
    _kernel32.CloseHandle.restype = wt.BOOL
else:  # pragma: no cover - 本机是 Windows；保留可导入性
    _kernel32 = None


def _require_windows() -> None:
    if os.name != "nt" or _kernel32 is None:
        raise OSError("Job Object 仅支持 Windows")


def create_kill_on_close_job() -> int:
    """创建带 `KILL_ON_JOB_CLOSE` 的 Job，返回句柄（调用方负责 close_job）。"""
    _require_windows()
    handle = _kernel32.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())

    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = _kernel32.SetInformationJobObject(
        handle,
        JobObjectExtendedLimitInformation,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if not ok:
        error = ctypes.get_last_error()
        _kernel32.CloseHandle(handle)
        raise ctypes.WinError(error)
    return int(handle)


def assign_process(job: int, proc: subprocess.Popen) -> None:
    """spawn 后立即入 job（v1 接受毫秒级竞态窗口，见设计 §7.3.2）。"""
    _require_windows()
    if not job:
        raise OSError("无效的 job handle")
    process_handle = getattr(proc, "_handle", None)
    if process_handle is None:
        raise OSError("Popen 没有可用的进程句柄")
    if not _kernel32.AssignProcessToJobObject(job, int(process_handle)):
        raise ctypes.WinError(ctypes.get_last_error())


def terminate_job(job: int) -> None:
    """原子终止 job 内所有进程。"""
    _require_windows()
    if not job:
        return
    if not _kernel32.TerminateJobObject(job, 1):
        raise ctypes.WinError(ctypes.get_last_error())


def close_job(job: int) -> None:
    """关闭句柄；`KILL_ON_JOB_CLOSE` 生效，残留子进程随之一并终止。"""
    _require_windows()
    if not job:
        return
    if not _kernel32.CloseHandle(job):
        raise ctypes.WinError(ctypes.get_last_error())
