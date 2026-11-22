"""会话/worker 生命周期工具（M0 主线，契约 §7.4）。"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

from agents.context import current_chat_id, current_trace_id
from agents.task_kinds import TaskKindError, resolve_task_kind
from bus.envelope import new_event, stop_key, worker_stream
from bus.stream import Bus
from config import Settings
from contracts import TaskSpec, ulid
from store.sqlite import Store

logger = logging.getLogger("sessions")

CREATE_NEW_PROCESS_GROUP = 0x00000200
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259

# LangChain message.type → 面向模型的角色名
_ROLE_BY_TYPE = {
    "human": "user",
    "ai": "assistant",
    "tool": "tool",
    "system": "system",
    "function": "tool",
}


def _norm_session_id(value: str) -> str:
    value = (value or "").strip()
    return value if value.startswith("worker:") else f"worker:{value}"


def _content_text(content: Any) -> str:
    """把 str / 多模态内容块列表压成纯文本（图片块只留占位标记）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
                else:
                    parts.append(f"[{block.get('type') or 'block'}]")
            else:
                parts.append(str(block))
        return "\n".join(part for part in parts if part)
    return str(content or "")


def _message_view(message: Any, *, max_chars: int) -> dict:
    """单条消息 → 可序列化视图；超长按 max_chars 截断并标注。"""
    role = _ROLE_BY_TYPE.get(str(getattr(message, "type", "") or ""), "assistant")
    text = _content_text(getattr(message, "content", ""))
    view: dict[str, Any] = {"role": role}
    if role == "tool":
        view["tool_call_id"] = str(getattr(message, "tool_call_id", "") or "")
        view["name"] = str(getattr(message, "name", "") or "")
    calls = getattr(message, "tool_calls", None) or []
    if calls:
        view["tool_calls"] = [
            str(call.get("name") if isinstance(call, dict) else getattr(call, "name", "?"))
            for call in calls
        ]
    if max_chars > 0 and len(text) > max_chars:
        view["content"] = text[:max_chars]
        view["truncated"] = True
        view["content_chars"] = len(text)
    else:
        view["content"] = text
    return view


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


def _hard_kill_tree(pid: int) -> tuple[bool, str]:
    """第 3 级终止：`taskkill /F /T` 杀掉 worker 及其工具子进程。

    shell 工具内部仍用 Job Object（`tools/winjob.py`）终结单个工具进程树；
    跨进程的 worker 树只能靠 taskkill /T，两者是不同层级的双保险。
    """
    if not pid:
        return False, "no_pid"
    try:
        proc = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    ok = proc.returncode == 0 or not _pid_alive(pid)
    return ok, ((proc.stdout or "") + (proc.stderr or "")).strip()[:300]


class SessionService:
    def __init__(
        self,
        *,
        store: Store,
        bus: Bus,
        redis_client: Any,
        settings: Settings,
        repo_root: Path,
        checkpointer: Any = None,
    ) -> None:
        self.store = store
        self.bus = bus
        self.redis = redis_client
        self.settings = settings
        self.repo_root = repo_root
        # 与 router 共用同一个 PostgresSaver：checkpoint 按 thread_id 区分，
        # `worker:<task_id>` 的正文就是 worker 的完整对话记录（只读）。
        self.checkpointer = checkpointer

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
        # 空 prompt 会让 worker 起来后无事可做，且下面取标题会 IndexError；
        # 在入口按契约返回 error_class，不把原始异常抛给工具层。
        text_prompt = prompt if isinstance(prompt, str) else ""
        if not text_prompt.strip():
            return {
                "ok": False,
                "error_class": "empty_prompt",
                "message": "prompt 为空：必须说明要 worker 做什么。",
                "hint": "把任务目标、产出与边界写清楚再派发；不要用空白字符串占位。",
            }
        prompt = text_prompt

        # 类型注册表是唯一事实来源：未知类型在这里显式失败，
        # 不创建会话、不落盘、不拉起进程，也不静默回落到 general。
        try:
            spec = resolve_task_kind(task_kind)
        except TaskKindError as exc:
            return {"ok": False, "error_class": exc.error_class, "message": str(exc)}

        task_id = ulid()
        session_id = _norm_session_id(task_id)
        work_dir = Path(cwd) if cwd else Path(spec.default_cwd(self.settings))
        try:
            work_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return {"ok": False, "error_class": "cwd_invalid", "message": str(exc)}

        first_line = prompt.strip().splitlines()[0]
        title = (title or first_line[:40] or "未命名任务").strip()
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
            # 必须在这里固化会话归属：worker_result 回来时批次里没有 user_message，
            # 不能依赖全局 last_chat_id 兜底（多会话会把通知发给"最后一个说话的人"）。
            chat_id=current_chat_id.get() or "",
            tools_profile=tools_profile,  # type: ignore[arg-type]
            task_kind=spec.kind,          # type: ignore[arg-type]
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

    # ------------------------------------------------------------------ checkpoint
    def _checkpoint_state(self, task_id: str) -> tuple[Any, dict | None, str]:
        """读 `worker:<task_id>` 的最新 checkpoint；返回 (state, metadata, error)。

        只读，不写、不续跑。没有 checkpointer / 没有该线程时 `state=None`，
        `error` 说明原因（`checkpointer_unavailable` / `checkpoint_missing`）。
        """
        saver = self.checkpointer
        if saver is None:
            return None, None, "checkpointer_unavailable"
        try:
            tup = saver.get_tuple({"configurable": {"thread_id": f"worker:{task_id}"}})
        except Exception as exc:  # 连接断了也不能把工具调用打崩
            logger.warning("读取 checkpoint 失败 task=%s", task_id, exc_info=True)
            return None, None, f"{type(exc).__name__}: {exc}"
        if tup is None:
            return None, None, "checkpoint_missing"
        return tup.checkpoint or None, (tup.metadata or {}), ""

    def _result_text(self, task_id: str) -> tuple[str, str]:
        """`workers/<task_id>/result.json` 的最终结论（checkpoint 不可用时的兜底）。"""
        path = self.settings.workers_dir / task_id / "result.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "", ""
        return str(payload.get("text") or ""), str(payload.get("status") or "")

    def read_session(
        self,
        session_id: str,
        *,
        cursor: str | None = None,
        limit: int = 20,
        include_tools: bool = False,
    ) -> dict:
        sid = _norm_session_id(session_id)
        session = self.store.get_session(sid)
        if not session:
            return {"ok": False, "error_class": "not_found", "message": f"会话不存在: {sid}"}
        task_id = sid.split(":", 1)[1]

        raw_cursor = "" if cursor is None else str(cursor).strip()
        if raw_cursor and not raw_cursor.isdigit():
            return {
                "ok": False,
                "error_class": "invalid_cursor",
                "message": f"cursor 必须是上次返回的 next_cursor: {cursor!r}",
            }
        offset = int(raw_cursor or 0)
        cap = max(1, min(int(limit), 200))
        # 单条正文上限：复用处 §3.6 的窗口内单条 ToolMessage 上限，避免一次回吐整段日志。
        max_chars = max(200, int(self.settings.context_tool_message_max_chars))

        state, _metadata, error = self._checkpoint_state(task_id)
        messages: list[Any] = []
        if state is not None:
            messages = list((state.get("channel_values") or {}).get("messages") or [])
        if not include_tools:
            messages = [m for m in messages if getattr(m, "type", "") != "tool"]

        total = len(messages)
        end = max(0, total - offset)
        start = max(0, end - cap)
        window = messages[start:end]
        next_cursor = str(offset + len(window)) if start > 0 else None

        payload: dict[str, Any] = {
            "ok": True,
            "session_id": session.id,
            "title": session.title,
            "status": session.status,
            "cwd": session.cwd,
            "pid": session.pid,
            "pid_alive": _pid_alive(session.pid),
            "first_message_at": session.first_message_at,
            "source": "checkpoint" if state is not None else "none",
            "total": total,
            "returned": len(window),
            "next_cursor": next_cursor,
            "messages": [_message_view(m, max_chars=max_chars) for m in window],
        }
        if state is None:
            # 没有 Postgres 时退化到 result.json：至少让 router 拿得到最终结论。
            text, result_status = self._result_text(task_id)
            if text:
                payload["source"] = "result_json"
                payload["result_status"] = result_status
                payload["messages"] = [
                    _message_view(
                        type("_Msg", (), {"type": "ai", "content": text, "tool_calls": None})(),
                        max_chars=max_chars,
                    )
                ]
                payload["total"] = payload["returned"] = 1
                payload["note"] = "checkpoint 不可用，正文取自 result.json（只有最终结论，没有过程）。"
            else:
                payload["note"] = (
                    f"读不到会话正文（{error}）；该 worker 可能尚未跑到第一轮模型调用，"
                    "或本进程未配置 POSTGRES_DSN。结果仍以 worker_result 事件为准。"
                )
        return payload

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

        task_id = sid.split(":", 1)[1]
        state, metadata, error = self._checkpoint_state(task_id)
        last_checkpoint: dict[str, Any] | None = None
        if state is not None:
            max_chars = 4000 if str(detail).lower() == "full" else 300
            messages = list((state.get("channel_values") or {}).get("messages") or [])
            last_checkpoint = {
                "checkpoint_id": str(state.get("id") or ""),
                "step": int(metadata.get("step") or 0),
                "created_at": str(state.get("ts") or ""),
                "message_count": len(messages),
                "last_message": _message_view(messages[-1], max_chars=max_chars) if messages else None,
            }

        payload: dict[str, Any] = {
            "ok": True,
            "status": "has_update",
            "session_id": sid,
            "session_status": session.status,
            "pid_alive": _pid_alive(session.pid),
            "first_message_at": session.first_message_at,
            "last_checkpoint": last_checkpoint,
        }
        if last_checkpoint is None:
            text, result_status = self._result_text(task_id)
            payload["note"] = (
                f"读不到 checkpoint（{error}）；完整结果见 worker_result 事件"
                + (f"，result.json 状态 {result_status}" if result_status else "")
                + "。"
            )
            if text:
                payload["last_message_preview"] = text[:300]
        return payload

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
            "note": "已写入 worker 指令流；worker 下一轮模型调用前会读到并执行。",
        }

    def stop_command(self, task_id: str) -> dict:
        """三级终止（§5.5）：先礼后兵。

        - 第 1 次调用：写 `stop_key=1`，允许当前工具跑完；worker 注入收尾指令后
          会把 key 回写为 `2`（第 2 级）；
        - 已经礼过一次、进程仍在：直接第 3 级 `taskkill /F /T`；
        - 进程已退出：如实返回 `already_exited`，不重复杀。
        """
        session_id = _norm_session_id(task_id)
        session = self.store.get_session(session_id)
        if not session:
            return {"ok": False, "error_class": "not_found", "message": f"会话不存在: {session_id}"}
        bare = session_id.split(":", 1)[1]
        key = stop_key(bare)
        pid = session.pid

        if not _pid_alive(pid):
            self.redis.delete(key)
            return {
                "ok": True,
                "level_reached": 2,
                "state": "already_exited",
                "task_id": bare,
                "note": "worker 进程已退出，无需终止；结果以 worker_result 为准。",
            }

        current = str(self.redis.get(key) or "")
        if current in ("", "0"):
            self.redis.set(key, "1", ex=3600)
            deadline = time.monotonic() + max(0.0, float(self.settings.stop_graceful_wait_s))
            level = 1
            while time.monotonic() < deadline:
                if str(self.redis.get(key) or "") == "2":
                    level = 2
                    break
                if not _pid_alive(pid):
                    level = 2
                    break
                time.sleep(0.1)
            return {
                "ok": True,
                "level_reached": level,
                "state": "stopping" if level == 1 else "wrap_up_injected",
                "task_id": bare,
                "note": (
                    "已请求停止：当前工具会跑完，随后注入收尾指令。"
                    "若仍不结束，再次调用 StopCommand 将强制终止（第 3 级）。"
                ),
            }

        # 已经礼过一次，进程还活着 → 兵。
        self.redis.set(key, "3", ex=3600)
        killed, detail = _hard_kill_tree(pid)
        logger.warning("StopCommand 升级到第 3 级 task=%s pid=%s ok=%s detail=%s", bare, pid, killed, detail)
        return {
            "ok": killed,
            "level_reached": 3,
            "state": "hard_killed" if killed else "kill_failed",
            "task_id": bare,
            "pid": pid,
            "detail": detail,
            "note": "已执行 taskkill /F /T；结果以 worker_result（或缺失）为准。",
        }


def build_session_tools(service: SessionService) -> list[Any]:
    """按契约导出 router 会话工具（最小集）。"""

    @tool
    def create_session(
        prompt: str,
        title: str | None = None,
        cwd: str | None = None,
        task_kind: str = "general",
    ) -> dict:
        """派发一个 worker 执行任务。只返回 accepted，没有执行结果；不要等待。

        task_kind：general（默认，通用执行）/ memory_consolidate（记忆整理）/
        cua（纯视觉 Computer Use，单屏，§8）。
        """
        return service.create_session(prompt, title=title, cwd=cwd, task_kind=task_kind)

    @tool
    def list_sessions(status: str | None = None, limit: int = 20) -> dict:
        """列出最近的 worker 会话（可按 running/done/error 过滤）。"""
        return {"ok": True, "sessions": service.list_sessions(status=status, limit=limit)}

    @tool
    def read_session(
        session_id: str,
        limit: int = 20,
        cursor: str | None = None,
        include_tools: bool = False,
    ) -> dict:
        """读取某个 worker 会话的正文（来自 Postgres checkpoint，只读）。

        Args:
            session_id: 会话 id，`worker:<ulid>` 或裸 `<ulid>` 都接受。
            limit: 本次返回多少条消息（默认 20，最多 200），从最新往回取。
            cursor: 上一次返回的 `next_cursor`；回看更早的页时原样传回。
            include_tools: 是否包含工具返回内容（默认 False，只看对话正文）。

        返回 `messages`（role/content，单条超长会截断并标 `truncated`）、
        `total`/`returned`/`next_cursor`。读不到 checkpoint 时 `source="none"` 并说明原因。
        """
        return service.read_session(
            session_id, limit=limit, cursor=cursor, include_tools=include_tools
        )

    @tool
    def TaskOutput(session_id: str, detail: str = "summary") -> dict:
        """查询任务的最新 checkpoint 摘要。没有回报时返回 no_update，不要反复轮询。

        Args:
            session_id: 会话 id。
            detail: summary（默认，末条正文最多 300 字）或 full（最多 4000 字）。
        """
        return service.task_output(session_id, detail=detail)

    @tool
    def StopCommand(task_id: str) -> dict:
        """请求终止一个 worker 任务（v1 只写一级停止标记）。"""
        return service.stop_command(task_id)

    @tool
    def send_message_to_session(session_id: str, text: str) -> dict:
        """向某个 worker 追加一条指令（v1 仅写入其指令流，worker 尚未消费）。"""
        return service.send_message_to_session(session_id, text)


    return [
        create_session,
        list_sessions,
        read_session,
        TaskOutput,
        StopCommand,
        send_message_to_session,
    ]
