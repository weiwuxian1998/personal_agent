"""会话/worker 生命周期工具（M0 主线，契约 §7.4）。"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

from agents.context import current_trace_id
from bus.envelope import new_event, stop_key, worker_stream
from bus.stream import Bus
from config import Settings
from contracts import TaskSpec, ulid
from store.sqlite import Store

CREATE_NEW_PROCESS_GROUP = 0x00000200
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259


def _norm_session_id(value: str) -> str:
    value = (value or "").strip()
    return value if value.startswith("worker:") else f"worker:{value}"


def _process_started_at(pid: int) -> str:
    if os.name != "nt":
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return ""
    try:
        created, exited, kernel, user = (wt.FILETIME() for _ in range(4))
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return ""
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        seconds = ticks / 10_000_000 - 11_644_473_600   # FILETIME → Unix epoch
        return (
            datetime.fromtimestamp(seconds, timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    finally:
        kernel32.CloseHandle(handle)


def _pid_alive(pid: int | None) -> bool:
    if not pid or os.name != "nt":
        return False
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    try:
        code = wt.DWORD()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


class SessionService:
    def __init__(
        self,
        *,
        store: Store,
        bus: Bus,
        redis_client: Any,
        settings: Settings,
        repo_root: Path,
    ) -> None:
        self.store = store
        self.bus = bus
        self.redis = redis_client
        self.settings = settings
        self.repo_root = repo_root

    # ------------------------------------------------------------------ create
    def create_session(
        self,
        prompt: str,
        *,
        title: str | None = None,
        cwd: str | None = None,
        task_kind: str = "general",
        tools_profile: str = "worker",
    ) -> dict:
        task_id = ulid()
        session_id = _norm_session_id(task_id)
        work_dir = Path(cwd) if cwd else self.settings.workspace_root
        try:
            work_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return {"ok": False, "error_class": "cwd_invalid", "message": str(exc)}

        title = (title or prompt.strip().splitlines()[0][:40] or "未命名任务").strip()
        self.store.upsert_session(
            session_id,
            title=title,
            status="running",
            cwd=str(work_dir),
            first_message_at=None,
        )

        task = TaskSpec(
            task_id=task_id,
            prompt=prompt,
            title=title,
            trace_id=current_trace_id.get() or ulid(),
            cwd=str(work_dir),
            workspace_root=str(self.settings.workspace_root),
            tools_profile=tools_profile,  # type: ignore[arg-type]
            task_kind=task_kind,          # type: ignore[arg-type]
        )
        task_dir = self.settings.workers_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "task.json").write_text(task.to_json(), encoding="utf-8")

        log_path = self.settings.logs_dir / f"worker-{task_id}.log"
        env = os.environ.copy()
        try:
            with open(log_path, "ab") as log_file:
                proc = subprocess.Popen(
                    [
                        self.settings.python_exe(),
                        "-u",   # 日志实时落盘：父进程已把 stdout 重定向到 workers 日志
                        str(self.repo_root / "worker_runner.py"),
                        "--task",
                        task_id,
                    ],
                    cwd=str(self.repo_root),
                    env=env,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    creationflags=CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                )
        except OSError as exc:
            self.store.set_status(session_id, "error")
            return {"ok": False, "error_class": "spawn_failed", "message": str(exc), "task_id": task_id}

        started_at = _process_started_at(proc.pid)
        self.store.set_pid(session_id, proc.pid, started_at)
        return {
            "ok": True,
            "task_id": task_id,
            "session_id": session_id,
            "status": "accepted",
            "note": "任务已交给 worker；结果会以新事件回到上下文。此处没有结果，不要等待或轮询。",
        }

    # ------------------------------------------------------------------ read
    def list_sessions(self, *, status: str | None = None, limit: int = 20) -> list[dict]:
        return [
            {
                "session_id": s.id,
                "title": s.title,
                "status": s.status,
                "cwd": s.cwd,
                "pid": s.pid,
                "updated_at": s.updated_at,
                "first_message_at": s.first_message_at,
            }
            for s in self.store.list_sessions(status=status, limit=limit)
        ]

    def read_session(self, session_id: str, *, limit: int = 20) -> dict:
        sid = _norm_session_id(session_id)
        session = self.store.get_session(sid)
        if not session:
            return {"ok": False, "error_class": "not_found", "message": f"会话不存在: {sid}"}
        return {
            "ok": True,
            "session_id": session.id,
            "title": session.title,
            "status": session.status,
            "cwd": session.cwd,
            "pid": session.pid,
            "pid_alive": _pid_alive(session.pid),
            "first_message_at": session.first_message_at,
            "messages": [],
            "note": "v1 worker 尚未写逐条 JSONL；结果以 worker_result 事件为准。",
        }

    def task_output(self, session_id: str, *, detail: str = "summary") -> dict:
        sid = _norm_session_id(session_id)
        session = self.store.get_session(sid)
        if not session:
            return {"ok": False, "error_class": "not_found", "message": f"会话不存在: {sid}"}
        if session.first_message_at is None:
            return {
                "ok": True,
                "status": "no_update",
                "session_id": sid,
                "note": "该任务还没有任何回报；不要轮询，本轮应当结束或去做别的事。",
            }
        return {
            "ok": True,
            "status": "has_update",
            "session_id": sid,
            "session_status": session.status,
            "pid_alive": _pid_alive(session.pid),
            "first_message_at": session.first_message_at,
            "last_checkpoint": None,
            "note": "v1 未实现 checkpoint 摘要读取；完整结果见 worker_result 事件。",
        }

    # ------------------------------------------------------------------ control
    def send_message_to_session(self, session_id: str, text: str) -> dict:
        sid = _norm_session_id(session_id)
        task_id = sid.split(":", 1)[1]
        if not self.store.get_session(sid):
            return {"ok": False, "error_class": "not_found", "message": f"会话不存在: {sid}"}
        self.bus.ensure_worker_group(task_id)
        event = new_event(
            "router_message",
            "router",
            {"task_id": task_id, "reply_to": ulid(), "text": text},
            trace_id=current_trace_id.get() or ulid(),
        )
        self.bus.xadd(event, stream=worker_stream(task_id), maxlen=200)
        return {
            "ok": True,
            "queued": True,
            "task_id": task_id,
            "note": "v1 worker 尚未消费 router_message；queued 仅表示写入指令流。",
        }

    def stop_command(self, task_id: str) -> dict:
        session_id = _norm_session_id(task_id)
        if not self.store.get_session(session_id):
            return {"ok": False, "error_class": "not_found", "message": f"会话不存在: {session_id}"}
        bare = session_id.split(":", 1)[1]
        self.redis.set(stop_key(bare), "1", ex=3600)
        return {
            "ok": True,
            "level_reached": 1,
            "task_id": bare,
            "note": "v1 只写入停止标记；收尾注入与 hard kill 中间件随后接入。",
        }

    def set_session_title(self, session_id: str, title: str) -> dict:
        sid = _norm_session_id(session_id)
        session = self.store.get_session(sid)
        if not session:
            return {"ok": False, "error_class": "not_found", "message": f"会话不存在: {sid}"}
        self.store.upsert_session(
            sid,
            title=title,
            status=session.status,
            cwd=session.cwd,
            first_message_at=session.first_message_at,
        )
        return {"ok": True, "session_id": sid, "title": title}

    def fork_session(self, session_id: str, *, at_message_id: str | None = None) -> dict:
        return {
            "ok": False,
            "error_class": "not_supported",
            "message": "fork_session 需要 LangGraph checkpoint 复制，v1 端到端阶段未实现。",
        }


def build_session_tools(service: SessionService) -> list[Any]:
    """按契约导出 router 会话工具（最小集）。"""

    @tool
    def create_session(prompt: str, title: str | None = None, cwd: str | None = None) -> dict:
        """派发一个 worker 执行任务。只返回 accepted，没有执行结果；不要等待。"""
        return service.create_session(prompt, title=title, cwd=cwd)

    @tool
    def list_sessions(status: str | None = None, limit: int = 20) -> dict:
        """列出最近的 worker 会话（可按 running/done/error 过滤）。"""
        return {"ok": True, "sessions": service.list_sessions(status=status, limit=limit)}

    @tool
    def read_session(session_id: str, limit: int = 20) -> dict:
        """读取某个 worker 会话的元信息；消息正文以 worker_result 事件为准。"""
        return service.read_session(session_id, limit=limit)

    @tool
    def TaskOutput(session_id: str, detail: str = "summary") -> dict:
        """查询任务是否有回报。没有回报时返回 no_update；不要反复轮询。"""
        return service.task_output(session_id, detail=detail)

    @tool
    def StopCommand(task_id: str) -> dict:
        """请求终止一个 worker 任务（v1 只写一级停止标记）。"""
        return service.stop_command(task_id)

    @tool
    def send_message_to_session(session_id: str, text: str) -> dict:
        """向某个 worker 追加一条指令（v1 仅写入其指令流，worker 尚未消费）。"""
        return service.send_message_to_session(session_id, text)

    @tool
    def set_session_title(session_id: str, title: str) -> dict:
        """修改会话标题。"""
        return service.set_session_title(session_id, title)

    @tool
    def fork_session(session_id: str, at_message_id: str | None = None) -> dict:
        """从某个会话分叉（v1 未实现，返回 not_supported）。"""
        return service.fork_session(session_id, at_message_id=at_message_id)

    return [
        create_session,
        list_sessions,
        read_session,
        TaskOutput,
        StopCommand,
        send_message_to_session,
        set_session_title,
        fork_session,
    ]
