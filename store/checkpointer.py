"""Postgres checkpointer 装配（LangGraph 对话/任务状态真相）。

契约 §5.1：只存 checkpoint，不建业务表；业务表在 SQLite（`store/sqlite.py`）。

两个必须的 psycopg 参数：
- `autocommit=True`：`PostgresSaver.setup()` 的建表/迁移需要立即生效；
- `prepare_threshold=0`：避免连接池复用 prepared statement 时与 pgbouncer
  或跨 schema 场景冲突（langgraph 官方建议）。

`saver.setup()` 是幂等的：表已存在时只补跑迁移，可每次启动都调。
退出时必须 `pool.close()`，否则 Windows 上残留连接会拖住进程。
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg_pool import ConnectionPool


@contextmanager
def open_checkpointer(
    dsn: str,
    *,
    min_size: int = 1,
    max_size: int = 8,
) -> Iterator[tuple[ConnectionPool, PostgresSaver]]:
    """打开连接池并返回 `(pool, saver)`；退出时自动关闭池。

    用法：
        with open_checkpointer(settings.pg_dsn()) as (pool, saver):
            graph = build_router(settings, saver, deps)

    测试隔离：如果不想污染已有库，可在 DSN 里带
    `?options=-csearch_path%3D<独立schema>`，`setup()` 的未限定表名就会落在该 schema。
    """
    pool = ConnectionPool(
        conninfo=dsn,
        min_size=min_size,
        max_size=max_size,
        open=True,
        timeout=10,
        kwargs={"autocommit": True, "prepare_threshold": 0},
    )
    try:
        pool.wait(timeout=10)
        saver = PostgresSaver(pool)
        saver.setup()  # 幂等：建表 + 追赶迁移
        yield pool, saver
    finally:
        pool.close()
