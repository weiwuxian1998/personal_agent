"""M-STORE 验收脚本：SQLite / JSONL / Redis Stream / Postgres checkpointer。

用法：
    python scripts/check_store.py

真连本机 Redis 与 Postgres（不做单测桩）：
- Redis 用例覆盖「入队 → 取出 → 不 ACK（模拟崩溃）→ XAUTOCLAIM 重新认领 → ACK」；
- Postgres 用例使用独立 schema `mstore_check`（通过 DSN search_path 隔离），
  不读写已有的 public 表；`POSTGRES_DSN` 缺失或连不上时按契约打印 `SKIP(postgres)`；
- SQLite / JSONL 用系统临时目录，结束后清理，不留数据。

退出码 0 表示全部通过。
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import redis  # noqa: E402

from bus.envelope import DEDUPE_FEISHU_PREFIX, ROUTER_STREAM, new_event, render_digest  # noqa: E402
from bus.stream import Bus, try_dedupe  # noqa: E402
from config import settings  # noqa: E402
from contracts import ulid  # noqa: E402
from memory.jsonl import JsonlWriter, append_turn, read_records  # noqa: E402
from store.checkpointer import open_checkpointer  # noqa: E402
from store.sqlite import REQUIRED_TABLES, Store, next_run_utc_after  # noqa: E402

FAILURES: list[str] = []
PG_SCHEMA = "mstore_check"


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def section(title: str) -> None:
    print(f"\n--- {title} ---")


def raw_query(db_path: Path, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# ----------------------------------------------------------------------------- SQLite
def check_sqlite(tmp: Path) -> None:
    section("SQLite")
    db_path = tmp / "db" / "assistant.db"
    store = Store(db_path)
    store.init_schema()
    store.init_schema()  # 幂等

    tables = set(store.table_names())
    missing = sorted(set(REQUIRED_TABLES) - tables)
    check("sqlite_tables", not missing, f"缺少: {missing}")
    check("sqlite_wal", store.journal_mode() == "wal", store.journal_mode())

    # sessions 生命周期
    sid_a = f"worker:{ulid()}"
    sid_b = f"worker:{ulid()}"
    store.upsert_session(sid_a, title="测试会话 A", status="running", cwd=str(tmp))
    store.upsert_session(sid_b, title="测试会话 B", status="idle")
    rec = store.get_session(sid_a)
    check("session_get", rec is not None and rec.title == "测试会话 A" and rec.status == "running")
    store.set_pid(sid_a, 4242, "2026-11-08T00:00:00.000Z")
    store.set_status(sid_a, "waiting_router")
    store.set_first_message_at(sid_a, "2026-11-08T00:00:05.000Z")
    rec = store.get_session(sid_a)
    check("session_pid", rec is not None and rec.pid == 4242 and rec.pid_started_at is not None)
    check("session_status", rec is not None and rec.status == "waiting_router")
    check("session_first_msg", rec is not None and rec.first_message_at == "2026-11-08T00:00:05.000Z")

    all_rows = store.list_sessions(limit=20)
    check("session_list", {r.id for r in all_rows} >= {sid_a, sid_b})
    waiting = store.list_sessions(status="waiting_router", limit=20)
    check("session_filter_status", [r.id for r in waiting] == [sid_a])
    page1 = store.list_sessions(limit=1)
    check("session_limit", len(page1) == 1)
    page2 = store.list_sessions(limit=1, cursor=1)
    check("session_cursor_offset", len(page2) == 1 and page2[0].id != page1[0].id)

    # upsert 不覆盖已存在的 pid
    store.upsert_session(sid_a, title="改名后的 A", status="running")
    rec = store.get_session(sid_a)
    check("upsert_keeps_pid", rec is not None and rec.pid == 4242 and rec.title == "改名后的 A")

    # runs
    run_id = ulid()
    store.create_run(run_id, session_id=sid_a, trigger="user_message", status="running", turn_id="turn-1")
    store.finish_run(run_id, status="done", exit_code=0)
    run = raw_query(db_path, "SELECT status, ended_at, exit_code FROM runs WHERE id=?", (run_id,))[0]
    check("run_finish", run["status"] == "done" and run["ended_at"] and run["exit_code"] == 0)

    # schedules：cron 下次时间计算（上海 09:00 = UTC 01:00）
    after = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)  # 上海 20:00
    nxt = next_run_utc_after("0 9 * * *", "Asia/Shanghai", after)
    check(
        "cron_next_utc",
        nxt == datetime(2026, 11, 9, 1, 0, tzinfo=timezone.utc),
        nxt.isoformat(),
    )
    nxt_every5 = next_run_utc_after("*/5 * * * *", "Asia/Shanghai", after)
    check("cron_step", nxt_every5.minute % 5 == 0 and nxt_every5 > after, nxt_every5.isoformat())

    sched_cron = store.create_schedule(cron="*/5 * * * *", prompt="定时提醒", timezone="Asia/Shanghai")
    check("schedule_cron_next", sched_cron.next_run_utc is not None and sched_cron.status == "active")
    at_past = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    sched_at = store.create_schedule(at=at_past, prompt="一次性提醒")
    due = store.list_due_schedules(datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"))
    check("schedule_due", any(s.id == sched_at.id for s in due))
    store.update_schedule_after_run(sched_at.id, next_run_utc=None, ran_at_utc=at_past)
    sched_after = [s for s in store.list_schedules(status="done") if s.id == sched_at.id]
    check("schedule_at_done", bool(sched_after))
    store.set_schedule_status(sched_cron.id, "paused")
    check("schedule_pause", store.list_schedules(status="paused")[0].id == sched_cron.id)

    # task_watch 去重 + resolve 后重开
    check("watch_first", store.mark_watch(sid_a, "stalled") == "first")
    check("watch_seen", store.mark_watch(sid_a, "stalled") == "seen")
    watch_rows = raw_query(db_path, "SELECT COUNT(*) AS c FROM task_watch WHERE task_id=?", (sid_a,))
    check("watch_unique_row", watch_rows[0]["c"] == 1)
    store.resolve_watch(sid_a, "stalled")
    check("watch_reopen", store.mark_watch(sid_a, "stalled") == "first")
    check("watch_other_reason", store.mark_watch(sid_a, "process_gone") == "first")

    # 观测：worker 式并发短连接写入
    def write_events(worker_index: int) -> None:
        for i in range(25):
            store.record_tool_event(
                trace_id=f"trace-{worker_index}",
                session_id=sid_a,
                tool="Shell",
                ok=(i % 2 == 0),
                duration_ms=100 + i,
                shell="pwsh",
                error_class=None if i % 2 == 0 else "exit_nonzero",
                fingerprint="fp001",
                retry_count=1,
                exit_code=0 if i % 2 == 0 else 1,
                args_hash="h001",
            )

    threads = [threading.Thread(target=write_events, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    count = raw_query(db_path, "SELECT COUNT(*) AS c FROM tool_events")[0]["c"]
    check("tool_events_concurrent", count == 100, f"实际 {count}")

    store.record_cost(trace_id="trace-x", scope="router", model="deepseek-flash",
                      input_tokens=100, cached_tokens=10, output_tokens=20,
                      cost_usd=0.0001)
    cost = raw_query(db_path, "SELECT price_version, ts_utc FROM cost_ledger")[0]
    check("cost_ledger", cost["price_version"] == "v1" and cost["ts_utc"].endswith("Z"))

    # 审批：正常裁决、重复裁决、过期
    token = f"tok-{ulid()}"
    store.create_pending_approval(token=token, trace_id="trace-a", tool="Shell",
                                  args_json='{"command":"rm -rf x"}', ttl_s=600)
    verdict = store.decide_approval(token, "denied", note="危险命令")
    check("approval_decide", verdict is not None and verdict["decision"] == "denied"
          and verdict["tool"] == "Shell" and verdict["note"] == "危险命令")
    check("approval_redecide_none", store.decide_approval(token, "approved") is None)
    log = raw_query(db_path, "SELECT decision, args_hash, note FROM approval_log")[0]
    check("approval_log", log["decision"] == "denied" and len(log["args_hash"]) == 16 and log["note"] == "危险命令")

    expired_token = f"tok-{ulid()}"
    store.create_pending_approval(token=expired_token, trace_id="trace-b", tool="Write",
                                  args_json="{}", ttl_s=600)
    raw_conn = sqlite3.connect(str(db_path), timeout=5.0)
    raw_conn.execute("UPDATE pending_approvals SET expires_at=? WHERE token=?",
                     ("2020-01-01T00:00:00.000Z", expired_token))
    raw_conn.commit()
    raw_conn.close()
    check("approval_expired", store.decide_approval(expired_token, "approved") is None)
    status_row = raw_query(db_path, "SELECT status FROM pending_approvals WHERE token=?", (expired_token,))[0]
    check("approval_expired_status", status_row["status"] == "expired")


# ----------------------------------------------------------------------------- JSONL
def check_jsonl(tmp: Path) -> None:
    section("JSONL")
    directory = tmp / "jsonl"
    writer = JsonlWriter(directory, "turns")
    seq1 = writer.append({"kind": "turn", "text": "第一条"})
    seq2 = writer.append({"kind": "turn", "text": "第二条"})
    check("jsonl_seq", (seq1, seq2) == (1, 2), f"{seq1},{seq2}")

    path = writer.current_path()
    check("jsonl_rotation_name", path.name.startswith("turns-") and path.suffix == ".jsonl", path.name)
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    records = [json.loads(ln) for ln in lines]
    check("jsonl_fields", all({"v", "seq", "ts"} <= set(r) for r in records))
    check("jsonl_one_line_each", len(records) == 2)

    seq3 = append_turn({"kind": "turn", "text": "第三条"}, directory=directory)
    check("jsonl_next_seq_new_writer", seq3 == 3, str(seq3))
    check("jsonl_append_only", len(path.read_text(encoding="utf-8").splitlines()) == 3)

    got = list(read_records(directory))
    check("jsonl_read_all", [r["seq"] for r in got] == [1, 2, 3])
    check("jsonl_read_since", [r["seq"] for r in read_records(directory, since_seq=2)] == [3])
    check("jsonl_read_limit", len(list(read_records(directory, limit=2))) == 2)


# ----------------------------------------------------------------------------- Redis
def check_redis() -> None:
    section("Redis Stream（真连本机）")
    client = redis.Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=10,
    )
    try:
        client.ping()
    except Exception as exc:  # pragma: no cover - 环境问题
        check("redis_connect", False, f"{type(exc).__name__}: {exc}")
        return
    check("redis_connect", True)

    bus = Bus(client)
    bus.ensure_router_group()
    bus.ensure_router_group()  # 幂等
    check("redis_group_idempotent", True)
    baseline_len = bus.xlen()

    # 去重键：同 key 只放行一次
    msg_id = f"mstore-check-{ulid()}"
    check("dedupe_first", try_dedupe(client, msg_id, ttl_s=60) is True)
    check("dedupe_second", try_dedupe(client, msg_id, ttl_s=60) is False)
    client.delete(f"{DEDUPE_FEISHU_PREFIX}{msg_id}")

    # 入队 → 取出（不 ACK，模拟进程崩溃）
    e_user = new_event("user_message", "feishu", {"message_id": msg_id, "text": "崩溃重放测试", "chat_id": "oc_test"})
    e_worker = new_event("worker_result", "worker", {"task_id": f"worker:{ulid()}", "text": "worker 完成"})
    e_timer = new_event("timer", "scheduler", {"schedule_id": ulid(), "prompt": "喝水"})
    ids = [bus.xadd(e) for e in (e_user, e_worker, e_timer)]

    batch = bus.read_batch(block_ms=1000, count=3)
    check("redis_read_batch", len(batch) == 3, f"实际 {len(batch)}")
    check("redis_event_roundtrip", [ev.type for _, ev in batch] == ["user_message", "worker_result", "timer"])
    check("redis_pel_nonempty", bus.pel_nonempty())

    digest = render_digest([ev for _, ev in batch])
    check("redis_digest", "崩溃重放测试" in digest and "ReplyUser" in digest)
    bus.ack(batch)
    check("redis_pel_empty_after_ack", not bus.pel_nonempty())

    # 真实崩溃链路：另起一个进程取出消息后不 ACK 直接退出，再由本进程 XAUTOCLAIM 认领
    crash_event = new_event("user_message", "feishu", {"text": "子进程崩溃后必须重放", "chat_id": "oc_test"})
    crash_id = bus.xadd(crash_event)
    child_code = (
        "import json, os, sys\n"
        "sys.path.insert(0, os.getcwd())\n"
        "import redis\n"
        "from bus.stream import Bus\n"
        "client = redis.Redis.from_url(os.environ['MSTORE_REDIS_URL'], decode_responses=True, socket_timeout=5)\n"
        "batch = Bus(client).read_batch(block_ms=1000, count=1)\n"
        "print(json.dumps([entry_id for entry_id, _ in batch]))\n"
        "sys.stdout.flush()\n"
        "os._exit(0)\n"  # 模拟崩溃：不做 ack、不清理
    )
    proc = subprocess.run(
        [sys.executable, "-c", child_code],
        cwd=str(ROOT),
        env={**os.environ, "MSTORE_REDIS_URL": settings.redis_url},
        capture_output=True,
        text=True,
        timeout=30,
    )
    child_ids = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() else []
    check("redis_child_crash", proc.returncode == 0 and child_ids == [crash_id],
          f"rc={proc.returncode} ids={child_ids} err={proc.stderr[-200:]}")
    check("redis_pel_left_by_crash", bus.pel_nonempty())

    claimed = bus.claim_pending(min_idle_ms=0, count=10)
    check("redis_claim_after_crash", [cid for cid, _ in claimed] == [crash_id],
          f"实际 {[cid for cid, _ in claimed]}")
    check("redis_claim_event_payload", claimed and claimed[0][1].payload.get("text") == "子进程崩溃后必须重放")
    bus.ack(claimed)
    check("redis_pel_empty_after_recovery", not bus.pel_nonempty())
    ids.append(crash_id)

    # drain_more：先取 1 条，再把剩余 2 条补进同一批
    ids2 = [bus.xadd(new_event("user_message", "feishu", {"text": f"drain-{i}", "chat_id": "oc_test"})) for i in range(3)]
    first = bus.read_batch(block_ms=1000, count=1)
    merged = bus.drain_more(first, debounce_ms=50, max_batch=10)
    check("redis_drain_more", len(merged) == 3, f"实际 {len(merged)}")
    bus.ack(merged)

    # 清理本轮测试条目，不删除组（组是契约要求的常驻基础设施）
    for entry_id in ids + ids2:
        client.xdel(ROUTER_STREAM, entry_id)
    check("redis_stream_restored", bus.xlen() == baseline_len, f"{bus.xlen()} vs {baseline_len}")


# ----------------------------------------------------------------------------- Postgres
def _pg_dsn() -> str:
    import os

    return os.environ.get("POSTGRES_DSN") or settings.postgres_dsn


def check_postgres() -> None:
    section("Postgres checkpointer（独立 schema 隔离）")
    dsn = _pg_dsn()
    if not dsn:
        print("SKIP(postgres)  未配置 POSTGRES_DSN")
        return

    import psycopg

    try:
        with psycopg.connect(dsn, connect_timeout=5, autocommit=True) as conn:
            conn.execute(f"CREATE SCHEMA IF NOT EXISTS {PG_SCHEMA}")
            public_before = conn.execute("SELECT COUNT(*) FROM public.checkpoints").fetchone()[0]
    except Exception as exc:
        print(f"SKIP(postgres)  连接失败: {type(exc).__name__}: {exc}")
        return
    check("pg_connect", True)

    sep = "&" if "?" in dsn else "?"
    isolated = f"{dsn}{sep}options=-csearch_path%3D{PG_SCHEMA}"
    try:
        with open_checkpointer(isolated, min_size=1, max_size=4) as (pool, saver):
            with pool.connection() as conn:
                search_path = conn.execute("SHOW search_path").fetchone()[0]
                check("pg_search_path", search_path == PG_SCHEMA, search_path)
                tables = conn.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema=%s ORDER BY 1",
                    (PG_SCHEMA,),
                ).fetchall()
                names = {row[0] for row in tables}
            check(
                "pg_checkpoint_tables",
                {"checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations"} <= names,
                str(sorted(names)),
            )
            saver.setup()  # 幂等：再跑一次不应报错

            from langgraph.graph import END, START, StateGraph

            from typing import TypedDict

            class _State(TypedDict, total=False):
                n: int

            def _node(state: dict) -> dict:
                return {"n": int(state.get("n", 0)) + 1}

            graph = StateGraph(_State)
            graph.add_node("bump", _node)
            graph.add_edge(START, "bump")
            graph.add_edge("bump", END)
            app = graph.compile(checkpointer=saver)

            thread_id = f"mstore-check-{ulid()}"
            config = {"configurable": {"thread_id": thread_id}}
            app.invoke({"n": 0}, config)
            loaded = saver.get_tuple(config)
            check("pg_checkpoint_roundtrip", loaded is not None and loaded.config["configurable"]["checkpoint_id"] != "")
            saver.delete_thread(thread_id)
        check("pg_pool_closed", pool.closed is True)
    except Exception as exc:
        check("pg_checkpointer", False, f"{type(exc).__name__}: {exc}")
        return

    try:
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            public_after = conn.execute("SELECT COUNT(*) FROM public.checkpoints").fetchone()[0]
    except Exception as exc:
        check("pg_public_untouched", False, str(exc))
        return
    check("pg_public_untouched", public_after == public_before, f"{public_before} → {public_after}")
    print(f"留下: Postgres schema `{PG_SCHEMA}`（checkpoint 表，测试 thread 已删除）")


# ----------------------------------------------------------------------------- main
def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="mstore_check_"))
    try:
        check_sqlite(tmp)
        check_jsonl(tmp)
        check_redis()
        check_postgres()
    finally:
        # 临时目录只含本次测试数据；删除避免留下垃圾
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{len(FAILURES)} failure(s)")
    if FAILURES:
        print("失败项: " + ", ".join(FAILURES))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
