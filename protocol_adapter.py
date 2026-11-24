"""Web UI 协议适配器：只读观测 + 网页发送入口。

定位：gateway 旁边的**独立进程**；除 `POST /api/messages` 外，不改变
gateway/router/worker 的行为。

设计约束（工程契约）：
- SQLite **只读**（`mode=ro` URI 短连接）：业务表只许 router 写（§5.2）；
- Redis 默认只读；唯一写入口是 `POST /api/messages`，它向 router stream
  XADD 一条 `user_message`，并直接调用飞书 OpenAPI 发一条 `【网页】` 镜像，
  让手机端也能看到网页里发出的内容。不建消费组、不进 PEL；
- **不创建任何新的 Redis 状态键**（§6.5）；
- checkpoint 只读（`saver.get_tuple`，与 tools/sessions.py 的 read_session 同款），
  不写、不续跑；
- 网页消息的 router 回复仍走既有 outbox → gateway → 飞书链路。

REST（全部 /api 前缀）：
  GET  /api/status                 机器人/网关/router/stream 状态快照
  POST /api/messages               网页发送：入队 router + 飞书镜像（写入口）
  GET  /api/files?path=&download=  只读文件预览/下载（workspace/inbox/tool_outputs/workers）
  GET  /api/sessions               worker 会话列表（SQLite + task.json + pid 存活）
  GET  /api/sessions/{id}          会话详情 + checkpoint 消息（分页，从尾部往前）
  GET  /api/sessions/{id}/log      worker 日志尾部
  GET  /api/router/context         router:main checkpoint 消息（分页）
  GET  /api/turns                  router 每轮记账（turns-*.jsonl）
  GET  /api/inbox                  router 入站事件记账（inbox-*.jsonl）
  GET  /api/schedules              定时任务表
  GET  /api/watches                巡检告警表
  GET  /api/events?stream=&count=  stream 历史尾部（XRANGE）

WS /ws 协议（JSON 帧）：
  server→client:
    {type:"hello", status:{...}, events:[{stream,entry_id,event},...]}  连接即发
    {type:"event", stream:"router"|"outbox", entry_id, event:{...}}    实时事件
    {type:"status", status:{...}}                                       每 3s 快照
    {type:"pong"}
  client→server:
    {type:"ping"}

运行：python protocol_adapter.py [--host 127.0.0.1] [--port 8765]
前端构建产物 ui/web/dist 存在时由本进程直接托管（/）。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import sqlite3
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

import redis
import requests
import uvicorn
from fastapi import Body, FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from bus.envelope import (
    OUTBOX_GROUP,
    OUTBOX_STREAM,
    ROUTER_HEARTBEAT_KEY,
    ROUTER_STATE_KEY,
    ROUTER_STREAM,
    ROUTER_STREAM_MAXLEN,
    event_from_redis,
    event_to_redis,
    new_event,
)
from channel.base import wrap_user_query
from config import settings
from contracts import now_iso, ulid
from memory.jsonl import read_records
from memory.layout import ledger_dir
from proc_utils import pid_alive
from store.checkpointer import open_checkpointer

REPO_ROOT = Path(__file__).resolve().parent
logger = logging.getLogger("adapter")

STATUS_INTERVAL_S = 3.0
REPLAY_BUFFER_MAX = 500
REPLAY_ON_CONNECT = 100
VIEW_MAX_CHARS = 8_000          # 单条消息正文上限（UI 展示用，比 router 工具宽）
TOOL_ARGS_PREVIEW_CHARS = 2_000

_ROLE_BY_TYPE = {
    "human": "user",
    "ai": "assistant",
    "tool": "tool",
    "system": "system",
    "function": "tool",
}


# ----------------------------------------------------------------------------- 只读 SQLite
def _ro_conn() -> sqlite3.Connection:
    """只读短连接（WAL 下与 router 的写并发安全）。

    Windows 下 `mode=ro` URI 参数在部分 Python 版本上会对 WAL/SHM 子文件
    触发 CANTOPEN / IOERR（URI 只控主文件，子文件用默认模式打开）。
    改用普通 connect + query_only pragma 更稳。
    """
    conn = sqlite3.connect(str(settings.db_path), timeout=5)
    conn.execute("PRAGMA query_only = ON")
    conn.row_factory = sqlite3.Row
    return conn


def _query(sql: str, params: tuple = ()) -> list[dict]:
    try:
        with contextlib.closing(_ro_conn()) as conn:
            return [dict(row) for row in conn.execute(sql, params)]
    except sqlite3.Error as exc:
        logger.warning("SQLite 只读查询失败: %s", exc)
        return []


def _task_meta(task_id: str) -> dict:
    """workers/<id>/task.json + result.json 的展示摘要（读不到就空）。"""
    out: dict[str, Any] = {}
    base = settings.workers_dir / task_id
    try:
        spec = json.loads((base / "task.json").read_text(encoding="utf-8"))
        out["prompt_preview"] = str(spec.get("prompt") or "")[:300]
        out["task_kind"] = spec.get("task_kind") or "general"
        out["chat_id"] = spec.get("chat_id") or ""
        out["created_at"] = spec.get("created_at") or ""
    except (OSError, ValueError):
        pass
    try:
        result = json.loads((base / "result.json").read_text(encoding="utf-8"))
        out["result_status"] = result.get("status") or ""
        out["result_preview"] = str(result.get("text") or "")[:300]
    except (OSError, ValueError):
        pass
    return out


def list_sessions_view(*, status: Optional[str], limit: int, offset: int) -> dict:
    sql = "SELECT * FROM sessions"
    params: list[Any] = []
    if status:
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    rows = _query(sql, tuple(params))
    total = _query("SELECT COUNT(*) AS c FROM sessions" + (" WHERE status = ?" if status else ""),
                   (status,) if status else ())
    items = []
    for row in rows:
        task_id = row["id"].split(":", 1)[1] if ":" in row["id"] else row["id"]
        item = dict(row)
        item["pid_alive"] = pid_alive(row.get("pid"))
        item.update(_task_meta(task_id))
        items.append(item)
    return {"ok": True, "total": total[0]["c"] if total else 0, "items": items}


def get_session_view(session_id: str) -> Optional[dict]:
    sid = session_id if session_id.startswith("worker:") else f"worker:{session_id}"
    rows = _query("SELECT * FROM sessions WHERE id = ?", (sid,))
    if not rows:
        return None
    row = rows[0]
    task_id = sid.split(":", 1)[1]
    out = dict(row)
    out["pid_alive"] = pid_alive(row.get("pid"))
    out.update(_task_meta(task_id))
    return out


# ----------------------------------------------------------------------------- checkpoint 只读
def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
                elif block.get("type") == "image_url":
                    parts.append("[图片]")
                else:
                    parts.append(f"[{block.get('type') or 'block'}]")
            else:
                parts.append(str(block))
        return "\n".join(p for p in parts if p)
    return str(content or "")


def _ui_message_view(message: Any, *, max_chars: int = VIEW_MAX_CHARS) -> dict:
    """LangChain 消息 → UI 视图（本模块私有 presentation，不是跨进程共享结构）。"""
    role = _ROLE_BY_TYPE.get(str(getattr(message, "type", "") or ""), "assistant")
    text = _content_text(getattr(message, "content", ""))
    view: dict[str, Any] = {"role": role, "id": str(getattr(message, "id", "") or "")}
    if role == "tool":
        view["tool_call_id"] = str(getattr(message, "tool_call_id", "") or "")
        view["name"] = str(getattr(message, "name", "") or "")
    calls = getattr(message, "tool_calls", None) or []
    if calls:
        view["tool_calls"] = []
        for call in calls:
            if isinstance(call, dict):
                name, args, call_id = call.get("name"), call.get("args"), call.get("id")
            else:
                name, args, call_id = getattr(call, "name", "?"), getattr(call, "args", None), getattr(call, "id", "")
            args_text = json.dumps(args, ensure_ascii=False) if isinstance(args, (dict, list)) else str(args or "")
            view["tool_calls"].append({
                "name": str(name or "?"),
                "id": str(call_id or ""),
                "args_preview": args_text[:TOOL_ARGS_PREVIEW_CHARS],
                "args_chars": len(args_text),
            })
    usage = getattr(message, "usage_metadata", None)
    if usage:
        view["usage"] = {
            "input_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
        }
    if max_chars > 0 and len(text) > max_chars:
        view["content"] = text[:max_chars]
        view["truncated"] = True
        view["content_chars"] = len(text)
    else:
        view["content"] = text
    return view


class CheckpointReader:
    """只读 checkpoint（get_tuple）；PG 不可用时整体降级为 source=none。"""

    def __init__(self) -> None:
        self.saver: Any = None
        self.error: str = ""

    def read(self, thread_id: str, *, cursor: Optional[str], limit: int, include_tools: bool) -> dict:
        offset = int(cursor or 0)
        cap = max(1, min(int(limit), 200))
        if self.saver is None:
            return {"ok": True, "thread_id": thread_id, "source": "none",
                    "note": f"checkpoint 不可用（{self.error or '未配置 POSTGRES_DSN'}）",
                    "total": 0, "returned": 0, "next_cursor": None, "messages": []}
        try:
            tup = self.saver.get_tuple({"configurable": {"thread_id": thread_id}})
        except Exception as exc:  # noqa: BLE001 - 观测接口不能因 PG 抖动崩掉
            return {"ok": True, "thread_id": thread_id, "source": "none",
                    "note": f"读取 checkpoint 失败: {type(exc).__name__}: {exc}",
                    "total": 0, "returned": 0, "next_cursor": None, "messages": []}
        if tup is None:
            return {"ok": True, "thread_id": thread_id, "source": "none",
                    "note": "该线程还没有 checkpoint",
                    "total": 0, "returned": 0, "next_cursor": None, "messages": []}
        state = tup.checkpoint or {}
        messages = list((state.get("channel_values") or {}).get("messages") or [])
        if not include_tools:
            messages = [m for m in messages if getattr(m, "type", "") != "tool"]
        total = len(messages)
        end = max(0, total - offset)
        start = max(0, end - cap)
        window = messages[start:end]
        views = [_ui_message_view(m) for m in window]
        return {
            "ok": True,
            "thread_id": thread_id,
            "source": "checkpoint",
            "checkpoint": {
                "id": str(state.get("id") or ""),
                "ts": str(state.get("ts") or ""),
                "step": int((tup.metadata or {}).get("step") or 0),
            },
            "total": total,
            "returned": len(views),
            "total_chars": sum(len(v.get("content") or "") for v in views),
            "next_cursor": str(offset + len(window)) if start > 0 else None,
            "messages": views,
        }


# ----------------------------------------------------------------------------- 状态快照
_gateway_cache: dict[str, Any] = {"ts": 0.0, "alive": None}


def _gateway_alive() -> Optional[bool]:
    """gateway 进程存活（命令行含 main_gateway.py）；5s 缓存，探测失败返回 None。"""
    now = time.monotonic()
    if now - _gateway_cache["ts"] < 5.0:
        return _gateway_cache["alive"]
    alive: Optional[bool] = None
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Where-Object { $_.CommandLine -like '*main_gateway.py*' } | Measure-Object).Count"],
            capture_output=True, text=True, timeout=10,
        )
        alive = int((proc.stdout or "0").strip() or 0) > 0
    except (OSError, subprocess.TimeoutExpired, ValueError):
        logger.warning("gateway 进程探测失败", exc_info=True)
    _gateway_cache.update(ts=now, alive=alive)
    return alive


def _stream_metrics(client: redis.Redis, stream: str, group: str) -> dict:
    out = {"xlen": 0, "lag": 0, "pending": 0}
    try:
        out["xlen"] = int(client.xlen(stream))
        for g in client.xinfo_groups(stream) or []:
            info = {(k.decode() if isinstance(k, bytes) else str(k)): v for k, v in g.items()}
            if info.get("name") != group:
                continue
            out["pending"] = int(info.get("pending") or 0)
            lag = info.get("lag")
            if lag is not None:
                out["lag"] = int(lag)
            else:
                # MAXLEN 裁剪后 lag=None：与 Bus.lag 同款兜底（契约 §14-33）
                last_delivered = str(info.get("last-delivered-id") or "0-0")
                info_s = client.xinfo_stream(stream) or {}
                last_generated = str(info_s.get("last-generated-id") or "")
                out["lag"] = 1 if last_generated and last_generated != last_delivered else out["pending"]
    except redis.ResponseError:
        pass
    except redis.RedisError as exc:
        logger.warning("stream 状态读取失败 %s: %s", stream, exc)
    return out


# ----------------------------------------------------------------------------- 飞书网页镜像
_FEISHU_TOKEN_CACHE: dict[str, Any] = {"token": "", "expires_at": 0.0}
_FEISHU_TOKEN_LOCK = threading.Lock()
_FEISHU_ECHO_MAX_CHARS = 4000


def _feishu_tenant_token() -> str:
    """取 tenant_access_token（缓存到过期前 60s）。失败抛 RuntimeError。"""
    now = time.monotonic()
    with _FEISHU_TOKEN_LOCK:
        cached = str(_FEISHU_TOKEN_CACHE.get("token") or "")
        if cached and now < float(_FEISHU_TOKEN_CACHE.get("expires_at") or 0.0):
            return cached
        resp = requests.post(
            f"{settings.feishu_domain.rstrip('/')}/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": settings.feishu_app_id, "app_secret": settings.feishu_app_secret},
            timeout=15,
        )
        try:
            data = resp.json()
        except ValueError:
            raise RuntimeError(f"token 接口返回非 JSON（HTTP {resp.status_code}）")
        if resp.status_code != 200 or int(data.get("code") or 0) != 0:
            raise RuntimeError(
                f"token 获取失败 HTTP {resp.status_code} code={data.get('code')}: {data.get('msg')}"
            )
        token = str(data.get("tenant_access_token") or "")
        if not token:
            raise RuntimeError("token 接口未返回 tenant_access_token")
        expire_s = max(int(data.get("expire") or 7200), 60)
        _FEISHU_TOKEN_CACHE.update(token=token, expires_at=now + expire_s - 60)
        return token


def _send_feishu_echo(chat_id: str, text: str) -> dict:
    """把网页里发出的用户原话，以机器人身份镜像到飞书会话。

    这样手机端能看到「网页端发了什么」以及后续助理回复。发送失败不阻断网页入队，
    由 `/api/messages` 把失败信息返回给前端展示。
    """
    if (settings.channel_kind or "").strip().lower() != "feishu":
        return {"ok": False, "error": "not_feishu_channel"}
    if not settings.feishu_app_id or not settings.feishu_app_secret:
        return {"ok": False, "error": "feishu_not_configured"}
    if not chat_id:
        return {"ok": False, "error": "missing_chat_id"}

    echo_text = f"【网页】{text}"
    if len(echo_text) > _FEISHU_ECHO_MAX_CHARS:
        echo_text = echo_text[: _FEISHU_ECHO_MAX_CHARS - 1] + "…"

    try:
        token = _feishu_tenant_token()
        resp = requests.post(
            f"{settings.feishu_domain.rstrip('/')}/open-apis/im/v1/messages"
            "?receive_id_type=chat_id",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={
                "receive_id": chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": echo_text}, ensure_ascii=False),
            },
            timeout=20,
        )
        try:
            data = resp.json()
        except ValueError:
            return {"ok": False, "error": f"feishu_http_{resp.status_code}_non_json"}
        if resp.status_code != 200 or int(data.get("code") or 0) != 0:
            msg = str(data.get("msg") or "")[:300]
            return {
                "ok": False,
                "error": f"feishu_code={data.get('code')}: {msg}",
                "code": data.get("code"),
            }
        return {
            "ok": True,
            "message_id": str((data.get("data") or {}).get("message_id") or ""),
        }
    except requests.RequestException as exc:
        return {"ok": False, "error": f"network_error: {exc}"}
    except Exception as exc:  # noqa: BLE001 - 镜像失败不能阻断网页消息
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def build_status(client: redis.Redis, reader: CheckpointReader) -> dict:
    router_state: dict[str, str] = {}
    heartbeat: Optional[str] = None
    redis_ok = True
    try:
        heartbeat = client.get(ROUTER_HEARTBEAT_KEY)
        router_state = {str(k): str(v) for k, v in (client.hgetall(ROUTER_STATE_KEY) or {}).items()}
    except redis.RedisError:
        redis_ok = False
    sessions_running = _query("SELECT COUNT(*) AS c FROM sessions WHERE status='running'")
    schedules_active = _query("SELECT COUNT(*) AS c FROM schedules WHERE status='active'")
    return {
        "ts": now_iso(),
        "redis_ok": redis_ok,
        "postgres_ok": reader.saver is not None,
        "channel_kind": settings.channel_kind,
        "gateway": {"alive": _gateway_alive()},
        "router": {
            "alive": bool(heartbeat),
            "heartbeat": heartbeat,
            "state": router_state,
            "stream": _stream_metrics(client, ROUTER_STREAM, "router"),
        },
        "outbox": {"stream": _stream_metrics(client, OUTBOX_STREAM, OUTBOX_GROUP)},
        "counts": {
            "sessions_running": sessions_running[0]["c"] if sessions_running else 0,
            "schedules_active": schedules_active[0]["c"] if schedules_active else 0,
        },
    }


# ----------------------------------------------------------------------------- 实时事件 hub
class StreamHub:
    """XREAD tail router/outbox（不建消费组）→ 环形缓冲 + 广播给 WS 订阅者。"""

    def __init__(self, redis_url: str) -> None:
        self._url = redis_url
        self._buffer: deque[dict] = deque(maxlen=REPLAY_BUFFER_MAX)
        self._subscribers: set[asyncio.Queue] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    # -- 订阅管理（event loop 线程调用）
    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def recent(self, count: int = REPLAY_ON_CONNECT) -> list[dict]:
        with self._lock:
            return list(self._buffer)[-count:]

    def _publish(self, item: dict) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                pass   # 慢客户端丢帧，不影响别人

    def broadcast(self, item: dict) -> None:
        """线程安全广播（tail 线程 / 状态 ticker 都走这里）。"""
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._publish, item)

    # -- tail 线程
    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._thread = threading.Thread(target=self._run, name="stream-tail", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=3)

    def _run(self) -> None:
        client = redis.from_url(self._url, decode_responses=True, socket_timeout=None)
        last_ids = {ROUTER_STREAM: "$", OUTBOX_STREAM: "$"}
        # 先用历史尾部填充回放缓冲，再从最后一条之后开始 tail
        try:
            for stream, count in ((ROUTER_STREAM, REPLAY_ON_CONNECT), (OUTBOX_STREAM, 50)):
                entries = client.xrevrange(stream, count=count) or []
                for entry_id, fields in reversed(entries):
                    self._append(stream, str(entry_id), fields, replay=True)
                if entries:
                    last_ids[stream] = str(entries[0][0])
        except redis.RedisError:
            logger.warning("回放缓冲初始化失败", exc_info=True)

        while not self._stop_event.is_set():
            try:
                result = client.xread(last_ids, block=2000, count=100)
            except redis.RedisError:
                logger.warning("XREAD tail 失败，1s 后重试", exc_info=True)
                self._stop_event.wait(1.0)
                continue
            for stream, entries in result or []:
                stream = str(stream)
                for entry_id, fields in entries:
                    last_ids[stream] = str(entry_id)
                    self._append(stream, str(entry_id), fields, replay=False)

    def _append(self, stream: str, entry_id: str, fields: dict, *, replay: bool) -> None:
        try:
            event = event_from_redis({str(k): v for k, v in fields.items()})
        except (ValueError, KeyError):
            logger.warning("无法解析 stream 条目 %s %s", stream, entry_id)
            return
        item = {"type": "event", "stream": "router" if stream == ROUTER_STREAM else "outbox",
                "entry_id": entry_id, "event": event.to_dict()}
        with self._lock:
            self._buffer.append(item)
        if not replay:
            self.broadcast(item)


# ----------------------------------------------------------------------------- FastAPI 装配
def create_app() -> FastAPI:
    app = FastAPI(title="personal-assistant adapter", version="1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"], allow_headers=["*"],
    )

    client = redis.from_url(settings.redis_url, decode_responses=True,
                            socket_timeout=settings.redis_socket_timeout_s)
    reader = CheckpointReader()
    hub = StreamHub(settings.redis_url)
    app.state.client = client
    app.state.reader = reader
    app.state.hub = hub

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        settings.ensure_dirs()
        stack = contextlib.ExitStack()
        try:
            if settings.postgres_dsn:
                _pool, saver = stack.enter_context(open_checkpointer(
                    settings.pg_dsn(), min_size=1, max_size=4))
                reader.saver = saver
            else:
                reader.error = "未配置 POSTGRES_DSN"
        except Exception as exc:  # noqa: BLE001 - PG 挂了也要能看其余状态
            reader.error = f"{type(exc).__name__}: {exc}"
            logger.warning("checkpointer 不可用，降级运行: %s", reader.error)
        hub.start(asyncio.get_running_loop())

        async def status_ticker() -> None:
            while True:
                await asyncio.sleep(STATUS_INTERVAL_S)
                try:
                    status = await asyncio.to_thread(build_status, client, reader)
                    hub.broadcast({"type": "status", "status": status})
                except Exception:  # noqa: BLE001
                    logger.warning("状态广播失败", exc_info=True)

        ticker = asyncio.create_task(status_ticker())
        try:
            yield
        finally:
            ticker.cancel()
            hub.stop()
            stack.close()
            client.close()

    app.router.lifespan_context = lifespan

    # ------------------------------------------------------------------ REST
    @app.get("/api/status")
    async def api_status() -> dict:
        return await asyncio.to_thread(build_status, client, reader)

    @app.post("/api/messages")
    async def api_send_message(payload: dict = Body(...)) -> dict:
        """网页发送：飞书镜像 + 写入 router stream。

        网页消息不带飞书 `om_` message_id，因此 `message_id` 留空，ReplyUser 不会
        尝试引用一条不存在的飞书消息；回复由 gateway 按 chat_id 直接发出。
        """
        text = str(payload.get("text") or "").strip()
        if not text:
            return JSONResponse(
                {"ok": False, "error": "empty_text", "message": "消息不能为空"},
                status_code=400,
            )
        if len(text) > 8000:
            return JSONResponse(
                {"ok": False, "error": "text_too_long", "message": "单条网页消息最多 8000 字"},
                status_code=400,
            )

        chat_id = str(payload.get("chat_id") or "").strip()
        if not chat_id:
            chat_id = str(
                await asyncio.to_thread(
                    lambda: client.hget(ROUTER_STATE_KEY, "last_chat_id") or ""
                )
            )
        if not chat_id:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "no_chat_id",
                    "message": "还没有可用的飞书会话；请先在飞书给机器人发一条消息",
                },
                status_code=400,
            )

        alive = await asyncio.to_thread(_gateway_alive)
        if alive is False:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "gateway_offline",
                    "message": "gateway 不在线，网页消息无法进入处理链路",
                },
                status_code=503,
            )

        # 先镜像到飞书，保证手机端先看到用户这条消息，再看到助理回复。
        echo = await asyncio.to_thread(_send_feishu_echo, chat_id, text)

        now_ms = int(time.time() * 1000)
        web_message_id = ulid()
        event = new_event(
            "user_message",
            "console",
            {
                "channel": "web",
                "origin": "web",
                "chat_id": chat_id,
                "chat_type": "p2p",
                "message_id": "",
                "web_message_id": web_message_id,
                "sender_id": "web-ui",
                "thread_id": None,
                "text": wrap_user_query(text, create_time_ms=now_ms),
                "create_time": now_ms,
            },
            trace_id=ulid(),
        )
        try:
            entry_id = await asyncio.to_thread(
                lambda: client.xadd(
                    ROUTER_STREAM,
                    event_to_redis(event),
                    maxlen=ROUTER_STREAM_MAXLEN,
                    approximate=True,
                )
            )
        except redis.RedisError as exc:
            logger.error("网页消息写入 router stream 失败: %s", exc)
            return JSONResponse(
                {
                    "ok": False,
                    "error": "enqueue_failed",
                    "message": f"写入 router stream 失败: {exc}",
                },
                status_code=503,
            )

        logger.info(
            "网页消息入队 chat=%s entry=%s chars=%d echo_ok=%s",
            chat_id,
            entry_id,
            len(text),
            bool(echo.get("ok")),
        )
        return {
            "ok": True,
            "entry_id": str(entry_id),
            "event_id": event.id,
            "web_message_id": web_message_id,
            "chat_id": chat_id,
            "text": text,
            "echo": echo,
            "event": event.to_dict(),
        }

    @app.get("/api/sessions")
    async def api_sessions(status: Optional[str] = None, limit: int = Query(50, le=200),
                           offset: int = 0) -> dict:
        return await asyncio.to_thread(list_sessions_view, status=status, limit=limit, offset=offset)

    @app.get("/api/sessions/{session_id}")
    async def api_session(session_id: str, cursor: Optional[str] = None,
                          limit: int = Query(50, le=200), include_tools: bool = True) -> dict:
        session = await asyncio.to_thread(get_session_view, session_id)
        if session is None:
            return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
        task_id = session["id"].split(":", 1)[1]
        context = await asyncio.to_thread(
            reader.read, f"worker:{task_id}", cursor=cursor, limit=limit, include_tools=include_tools)
        if context["source"] == "none":
            # checkpoint 不可用 → result.json 兜底（与 read_session 同语义）
            meta = session.get("result_preview")
            if meta:
                context["source"] = "result_json"
                context["messages"] = [{"role": "assistant", "content": session.get("result_preview", "")}]
                context["total"] = context["returned"] = 1
        return {"ok": True, "session": session, "context": context}

    @app.get("/api/sessions/{session_id}/log")
    async def api_session_log(session_id: str, bytes: int = Query(65536, le=1_048_576)) -> dict:
        task_id = session_id.split(":", 1)[1] if ":" in session_id else session_id
        path = settings.logs_dir / f"worker-{task_id}.log"
        if not path.exists():
            return {"ok": False, "error": "log_not_found", "path": str(path)}
        size = path.stat().st_size
        with open(path, "rb") as fh:
            if size > bytes:
                fh.seek(size - bytes)
            data = fh.read()
        return {"ok": True, "path": str(path), "size": size,
                "truncated": size > bytes, "text": data.decode("utf-8", errors="replace")}

    @app.get("/api/router/context")
    async def api_router_context(cursor: Optional[str] = None, limit: int = Query(50, le=200),
                                 include_tools: bool = True) -> dict:
        return await asyncio.to_thread(
            reader.read, settings.router_thread_id, cursor=cursor, limit=limit, include_tools=include_tools)

    @app.get("/api/turns")
    async def api_turns(limit: int = Query(50, le=500)) -> dict:
        def _read() -> list[dict]:
            records = [r for r in read_records(ledger_dir(settings)) if "digest" in r]
            return records[-limit:][::-1]
        return {"ok": True, "items": await asyncio.to_thread(_read)}

    @app.get("/api/inbox")
    async def api_inbox(limit: int = Query(100, le=1000)) -> dict:
        def _read() -> list[dict]:
            records = [r for r in read_records(ledger_dir(settings))
                       if "event_id" in r and "type" in r]
            return records[-limit:][::-1]
        return {"ok": True, "items": await asyncio.to_thread(_read)}

    @app.get("/api/schedules")
    async def api_schedules() -> dict:
        rows = await asyncio.to_thread(
            _query, "SELECT * FROM schedules ORDER BY created_at DESC LIMIT 200")
        return {"ok": True, "items": rows}

    @app.get("/api/watches")
    async def api_watches() -> dict:
        rows = await asyncio.to_thread(
            _query, "SELECT * FROM task_watch ORDER BY last_detected_at DESC LIMIT 100")
        return {"ok": True, "items": rows}

    @app.get("/api/events")
    async def api_events(stream: str = "router", count: int = Query(50, le=500)) -> dict:
        key = ROUTER_STREAM if stream == "router" else OUTBOX_STREAM
        def _read() -> list[dict]:
            out = []
            try:
                for entry_id, fields in client.xrevrange(key, count=count) or []:
                    event = event_from_redis({str(k): v for k, v in fields.items()})
                    out.append({"entry_id": str(entry_id), "event": event.to_dict()})
            except (redis.RedisError, ValueError) as exc:
                logger.warning("读取 stream 历史失败: %s", exc)
            return out
        return {"ok": True, "stream": stream, "items": await asyncio.to_thread(_read)}

    # ------------------------------------------------------------------ WebSocket
    # ------------------------------------------------------------------ 文件预览 / 下载
    @app.get("/api/files")
    async def api_file(path: str = Query(...), download: bool = False):
        """只读文件访问：仅允许 workspace / inbox / tool_outputs / workers 内的文件。

        路径先 resolve 再判断前缀，符号链接逃逸也会被拒绝。返回原始文件（inline），
        浏览器可直接预览图片/PDF；文本类由前端 fetch 后渲染。
        """
        raw = Path(str(path)).expanduser()
        if not raw.is_absolute():
            raw = Path(settings.workspace_root) / raw
        try:
            target = raw.resolve(strict=True)
        except (OSError, RuntimeError):
            return JSONResponse({"ok": False, "error": "not_found", "path": str(raw)}, status_code=404)
        roots = [
            Path(settings.workspace_root).resolve(),
            Path(settings.inbox_dir).resolve(),
            Path(settings.tool_outputs_dir).resolve(),
            Path(settings.workers_dir).resolve(),
        ]
        if not any(target == root or target.is_relative_to(root) for root in roots):
            return JSONResponse(
                {"ok": False, "error": "path_outside_roots", "path": str(target)},
                status_code=403,
            )
        if not target.is_file():
            return JSONResponse({"ok": False, "error": "not_a_file", "path": str(target)}, status_code=404)
        return FileResponse(
            str(target),
            filename=target.name,
            content_disposition_type="attachment" if download else "inline",
        )

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        queue = hub.subscribe()
        status = await asyncio.to_thread(build_status, client, reader)
        await ws.send_json({"type": "hello", "status": status, "events": hub_recent_safe(hub)})

        async def writer() -> None:
            while True:
                item = await queue.get()
                await ws.send_json(item)

        writer_task = asyncio.create_task(writer())
        try:
            while True:
                try:
                    msg = await ws.receive_json()
                except WebSocketDisconnect:
                    break
                if not isinstance(msg, dict):
                    continue
                kind = msg.get("type")
                if kind == "ping":
                    await ws.send_json({"type": "pong"})
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001 - 单连接异常不影响其它客户端
            logger.warning("WS 连接异常", exc_info=True)
        finally:
            writer_task.cancel()
            hub.unsubscribe(queue)

    def hub_recent_safe(h: StreamHub) -> list[dict]:
        return h.recent()

    # ------------------------------------------------------------------ 前端静态托管
    dist = REPO_ROOT / "ui" / "web" / "dist"
    if dist.is_dir() and (dist / "index.html").exists():
        app.mount("/", StaticFiles(directory=str(dist), html=True), name="ui")
        logger.info("托管前端构建产物: %s", dist)
    else:
        @app.get("/")
        async def index_placeholder() -> dict:
            return {"ok": True, "service": "personal-assistant adapter",
                    "note": "前端尚未构建（ui/web/dist 不存在）；API 可用，开发期用 Vite dev server。"}

    return app


def main() -> int:
    parser = argparse.ArgumentParser(description="Web UI 协议适配器（观测 + 网页发送）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（局域网访问用 0.0.0.0）")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    handlers: list = [logging.StreamHandler(sys.stdout)]

    # 文件日志：尝试写 data_dir/logs/adapter-live.log；无权限则静默跳过（沙箱场景）。
    try:
        log_dir = settings.data_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "adapter-live.log"
        handlers.append(logging.FileHandler(str(log_file), encoding="utf-8"))
    except (PermissionError, OSError):
        pass

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [adapter] %(message)s",
        handlers=handlers,
    )
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())