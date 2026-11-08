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

from agents.base import last_ai_text
from agents.context import current_task_id, current_trace_id
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


def _session_id(task_id: str) -> str:
    return f"worker:{task_id}"


def _load_task(task_id: str) -> TaskSpec:
    path = settings.workers_dir / task_id / "task.json"
    if not path.exists():
        raise FileNotFoundError(f"task.json 不存在: {path}")
    return TaskSpec.from_json(path.read_text(encoding="utf-8"))


def _run_agent(task: TaskSpec, *, checkpointer) -> str:
    graph = build_worker(task=task, settings=settings, checkpointer=checkpointer)
    result = graph.invoke(
        {"messages": [{"role": "user", "content": task.prompt}]},
        {
            "configurable": {"thread_id": f"worker:{task.task_id}"},
            "recursion_limit": settings.worker_recursion_limit,
        },
    )
    text = last_ai_text(result.get("messages") or [])
    if not text:
        text = "（worker 没有产出文本结论：可能是模型只发了工具调用，或输出为空）"
    return text


def _publish(redis_client: redis.Redis, task: TaskSpec, *, text: str, status: str) -> None:
    bus = Bus(redis_client)
    event = new_event(
        "worker_result",
        "worker",
        {
            "task_id": task.task_id,
            "session_id": _session_id(task.task_id),
            "title": task.title,
            "status": status,
            "text": text,
        },
        trace_id=task.trace_id,
    )
    bus.xadd(event, stream=ROUTER_STREAM, maxlen=ROUTER_STREAM_MAXLEN)


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

    redis_client = redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_timeout=settings.redis_socket_timeout_s,
    )
    store = Store(settings.db_path)
    store.init_schema()
    session_id = _session_id(task.task_id)

    text = ""
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
            text = _run_agent(task, checkpointer=saver)
        finally:
            if checkpointer_ctx is not None:
                checkpointer_ctx.__exit__(None, None, None)
    except Exception:
        status = "error"
        exit_code = 1
        detail = traceback.format_exc(limit=8)
        logger.error("任务失败\n%s", detail)
        text = f"任务执行失败（worker 进程内异常）：\n{detail.strip().splitlines()[-1]}"

    try:
        (settings.workers_dir / task.task_id / "result.json").write_text(
            json.dumps({"status": status, "text": text}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _publish(redis_client, task, text=text, status=status)
        store.set_status(session_id, status)
        store.set_first_message_at(session_id)
        logger.info("任务结束 task=%s status=%s 结果长度=%d", task.task_id, status, len(text))
    except Exception:
        logger.error("回传 worker_result 失败（router 将收不到结果）\n%s", traceback.format_exc())
        return 3

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
