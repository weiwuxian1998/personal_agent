"""Worker 进程入口：一个任务一个进程，结果以 `worker_result` 事件回到 router。

调用关系（契约 §9）：`router.create_session` → `Popen(worker_runner.py --task <id>)`
→ 本文件读 `workers/<task_id>/task.json` → 跑 worker agent → XADD `worker_result`
→ 退出。worker 没有 ReplyUser，也不直接联系用户。

失败约定：无论成功失败都**必须**回传 `worker_result`，否则 router 永远收不到回报。
失败时 `status="error"`，正文写清失败层与原因。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import traceback
from pathlib import Path

import redis

from agents.base import last_ai_text, stream_agent_steps
from agents.context import current_task_id, current_trace_id
from agents.worker.control import WorkerControl
from agents.worker.graph import build_worker
from bus.envelope import ROUTER_STREAM, ROUTER_STREAM_MAXLEN, new_event
from bus.stream import Bus
from config import settings
from contracts import TaskSpec
from store.checkpointer import open_checkpointer
from store.sqlite import Store

REPO_ROOT = Path(__file__).resolve().parent
logger = logging.getLogger("worker")


def _setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [worker] %(message)s",
        stream=sys.stdout,
    )
    # HTTP 客户端的逐请求 INFO 噪音降级；模型/工具的关键过程由 stream_agent_steps 落日志。
    for noisy in ("httpx", "httpcore", "openai", "langchain_openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _session_id(task_id: str) -> str:
    return f"worker:{task_id}"


def _load_task(task_id: str) -> TaskSpec:
    path = settings.workers_dir / task_id / "task.json"
    if not path.exists():
        raise FileNotFoundError(f"task.json 不存在: {path}")
    return TaskSpec.from_json(path.read_text(encoding="utf-8"))


def _run_agent(
    task: TaskSpec,
    *,
    checkpointer,
    control: WorkerControl | None = None,
    redis_client: redis.Redis | None = None,
    on_round: Any = None,
) -> str:
    graph = build_worker(
        task=task,
        settings=settings,
        checkpointer=checkpointer,
        control=control,
        redis_client=redis_client,
        on_round=on_round,
    )
    # stream 而非 invoke：模型思考、工具调用及结果逐步实时落日志，任务失败时日志里有现场。
    messages = stream_agent_steps(
        graph,
        {"messages": [{"role": "user", "content": task.prompt}]},
        {
            "configurable": {"thread_id": f"worker:{task.task_id}"},
            "recursion_limit": settings.worker_recursion_limit,
        },
        logger=logger,
    )
    text = last_ai_text(messages)
    if not text:
        text = "（worker 没有产出文本结论：可能是模型只发了工具调用，或输出为空）"
    return text


def _error_summary(exc: BaseException) -> str:
    """失败原因的稳定摘要：异常类名 + 消息。

    不能只取 traceback 最后一行——`GraphRecursionError` 的 traceback 末尾是
    "For troubleshooting, visit: https://..."，真正的原因行会被丢掉。
    """
    message = str(exc).strip() or "(no message)"
    return f"{type(exc).__name__}: {message}"


def _publish(
    redis_client: redis.Redis,
    task: TaskSpec,
    *,
    text: str,
    status: str,
    error: str = "",
    result_path: str = "",
) -> None:
    bus = Bus(redis_client)
    payload = {
        "task_id": task.task_id,
        "session_id": _session_id(task.task_id),
        "title": task.title,
        "status": status,
        "text": text,
        # 会话归属由 task.json 固化，router 用它设置 current_chat_id，
        # 不再依赖全局 last_chat_id（多会话时会发错人）。
        "chat_id": task.chat_id,
        # router 靠它识别「记忆整理完成」，回写 consolidated 记账。
        "task_kind": task.task_kind,
        # result.json 的绝对路径：digest 里正文被截断时，router 可用 Read 读全文对账。
        "result_path": result_path,
    }
    if error:
        payload["error"] = error
    event = new_event("worker_result", "worker", payload, trace_id=task.trace_id)
    bus.xadd(event, stream=ROUTER_STREAM, maxlen=ROUTER_STREAM_MAXLEN)


def _write_result(
    workers_dir: Path,
    task_id: str,
    *,
    status: str,
    text: str,
    error: str = "",
) -> str | None:
    """写 `workers/<task_id>/result.json`；失败时 `error` 必须是 `类名: 消息`。

    返回落盘的绝对路径，随 `worker_result` 事件的 `result_path` 字段带给 router。
    **失败返回 None**，调用方必须把 `result_path` 置空——绝不能把错误文本当成路径
    塞进事件，否则 router 会拿它去 Read，读到的是"路径不存在"而不是任务结论。
    写盘失败本身不阻断汇报：正文已经在事件的 `text` 里，router 只是拿不到全文副本。
    """
    payload: dict[str, str] = {"status": status, "text": text}
    if error:
        payload["error"] = error
    try:
        target = Path(workers_dir) / task_id / "result.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(target.resolve())
    except Exception:
        logger.error("写 result.json 失败 task_id=%s（result_path 置空）\n%s", task_id, traceback.format_exc())
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="执行一个 worker 任务")
    parser.add_argument("--task", required=True, help="task_id（ULID）")
    args = parser.parse_args()

    _setup_logging()
    settings.ensure_dirs()

    try:
        task = _load_task(args.task)
    except Exception:
        logger.error("读取任务失败\n%s", traceback.format_exc())
        return 2

    current_trace_id.set(task.trace_id)
    current_task_id.set(task.task_id)
    logger.info("开始任务 task=%s title=%s", task.task_id, task.title)

    # CUA：环境准备（启动内核）不是模型工具，由运行时按配置执行；只关闭本次拉起的实例。
    browser_state: dict | None = None
    if task.task_kind == "cua" and settings.cua_browser_autostart:
        try:
            from cua.browser import ensure_browser

            browser_state = ensure_browser(settings)
            logger.info("CUA 浏览器内核：%s", {k: v for k, v in browser_state.items() if k != "proc"})
        except Exception:
            logger.warning("启动 CUA 浏览器失败（继续执行，模型可操作现有桌面）\n%s", traceback.format_exc())

    def _stop_cua_browser() -> None:
        if browser_state:
            from cua.browser import stop_browser

            stop_browser(browser_state)

    redis_client = redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_timeout=settings.redis_socket_timeout_s,
    )
    store = Store(settings.db_path)
    store.init_schema()
    session_id = _session_id(task.task_id)

    # 指令/停止通道：后台线程消费 worker 自己的 stream + 轮询 stop_key。
    control = WorkerControl(
        redis_client=redis_client,
        task_id=task.task_id,
        poll_interval_s=settings.worker_inbox_poll_s,
        inbox_max=settings.worker_inbox_max,
    )
    control.start()

    text = ""
    error = ""
    status = "done"
    exit_code = 0
    try:
        checkpointer_ctx = None
        try:
            if settings.postgres_dsn:
                # checkpointer 只影响「任务可恢复」，不参与业务正确性；连不上时如实降级。
                checkpointer_ctx = open_checkpointer(
                    settings.pg_dsn(),
                    min_size=settings.postgres_pool_min,
                    max_size=settings.postgres_pool_max,
                )
                pool, saver = checkpointer_ctx.__enter__()
            else:
                saver = None
                logger.warning("未配置 POSTGRES_DSN：worker 以无 checkpoint 模式运行")
        except Exception:
            logger.warning("Postgres checkpointer 不可用，降级为无 checkpoint\n%s", traceback.format_exc())
            saver = None

        try:
            text = _run_agent(
                task,
                checkpointer=saver,
                control=control,
                redis_client=redis_client,
                on_round=lambda: store.touch_session(session_id),
            )
        finally:
            if checkpointer_ctx is not None:
                checkpointer_ctx.__exit__(None, None, None)
            control.close()
    except Exception as exc:
        control.close()
        status = "error"
        exit_code = 1
        detail = traceback.format_exc(limit=8)
        logger.error("任务失败\n%s", detail)
        error = _error_summary(exc)
        text = f"任务执行失败（worker 进程内异常）：\n{error}"

    try:
        # 落盘失败时 result_path 为空串（_write_result 返回 None），事件里退化为
        # "没有全文副本"，而不是一个看起来像路径的错误文本（审计 P1 里那条）。
        result_path = _write_result(settings.workers_dir, task.task_id, status=status, text=text, error=error) or ""
        _publish(redis_client, task, text=text, status=status, error=error, result_path=result_path)
        store.set_status(session_id, status)
        store.set_first_message_at(session_id)
        logger.info("任务结束 task=%s status=%s 结果长度=%d", task.task_id, status, len(text))
    except Exception:
        logger.error("回传 worker_result 失败（router 将收不到结果）\n%s", traceback.format_exc())
        _stop_cua_browser()
        return 3

    _stop_cua_browser()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
