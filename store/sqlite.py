"""SQLite 存储层（`sessions` / `runs` / `schedules` / `task_watch` / 观测 / 审批）。

契约 §5.2：
- `PRAGMA journal_mode=WAL; synchronous=NORMAL; busy_timeout=5000`；
- 只有 router 进程写整库；worker 只调用 `record_tool_event` / `record_cost`
  （两者各自用短连接，WAL + busy_timeout 保证并发不阻塞）；
- DDL 字段名/类型与契约完全一致（这里只加了 `IF NOT EXISTS` 保证 `init_schema` 幂等）。

线程模型：`Store` 本身无共享连接，每个方法用一条短连接，可被多线程同时调用。
时间字段统一 ISO8601 字符串：展示/会话用 `now_iso()`（+08:00），
调度比较用 `now_utc_iso()`（UTC、Z 结尾），比较一律走 `contracts.parse_iso`。
"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Literal
from zoneinfo import ZoneInfo

from contracts import ScheduleRecord, SessionRecord, now_iso, now_utc_iso, parse_iso, ulid

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL,
  cwd TEXT, pid INTEGER, pid_started_at TEXT, parent_id TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  first_message_at TEXT, last_seen TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status, updated_at);

CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
  trigger TEXT NOT NULL, status TEXT NOT NULL,
  started_at TEXT NOT NULL, ended_at TEXT, exit_code INTEGER, turn_id TEXT
);

CREATE TABLE IF NOT EXISTS schedules (
  id TEXT PRIMARY KEY, cron TEXT, at TEXT,
  timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
  prompt TEXT NOT NULL, status TEXT NOT NULL,
  next_run_utc TEXT, last_run_utc TEXT,
  misfire_grace_s INTEGER NOT NULL DEFAULT 300,
  catch_up TEXT NOT NULL DEFAULT 'once',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sched_due ON schedules(status, next_run_utc);

CREATE TABLE IF NOT EXISTS task_watch (
  task_id TEXT NOT NULL REFERENCES sessions(id),
  reason TEXT NOT NULL, status TEXT NOT NULL,
  first_detected_at TEXT NOT NULL, last_detected_at TEXT NOT NULL,
  alerted_at TEXT, dedupe_key TEXT NOT NULL,
  PRIMARY KEY (task_id, reason)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_watch_dedup ON task_watch(dedupe_key);

CREATE TABLE IF NOT EXISTS cost_ledger (
  id TEXT PRIMARY KEY, ts_utc TEXT NOT NULL, trace_id TEXT,
  scope TEXT NOT NULL, model TEXT NOT NULL,
  input_tokens INTEGER NOT NULL, cached_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL, cost_usd REAL NOT NULL, price_version TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_approvals (
  token TEXT PRIMARY KEY, trace_id TEXT, tool TEXT NOT NULL, args_json TEXT NOT NULL,
  created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS approval_log (
  id TEXT PRIMARY KEY, trace_id TEXT, tool TEXT NOT NULL, args_hash TEXT NOT NULL,
  args_json TEXT, decision TEXT NOT NULL, decided_at TEXT, note TEXT
);

CREATE TABLE IF NOT EXISTS tool_events (
  id TEXT PRIMARY KEY, trace_id TEXT, session_id TEXT, tool TEXT NOT NULL,
  shell TEXT, error_class TEXT, fingerprint TEXT,
  retry_count INTEGER NOT NULL DEFAULT 0, exit_code INTEGER,
  ok INTEGER NOT NULL, duration_ms INTEGER NOT NULL, args_hash TEXT, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tool_events_fp ON tool_events(fingerprint, created_at);
CREATE INDEX IF NOT EXISTS idx_tool_events_class ON tool_events(error_class, created_at);

CREATE TABLE IF NOT EXISTS allowed_roots (path TEXT PRIMARY KEY, added_at TEXT NOT NULL);
"""

REQUIRED_TABLES = (
    "sessions",
    "runs",
    "schedules",
    "task_watch",
    "cost_ledger",
    "pending_approvals",
    "approval_log",
    "tool_events",
    "allowed_roots",
)


# ----------------------------------------------------------------------------- cron（仅标准库）
def _expand_field(field: str, low: int, high: int) -> tuple[set[int], bool]:
    """展开一个 cron 字段，返回 (取值集合, 是否"受限")。

    支持 `*`、`a`、`a-b`、`*/n`、`a-b/n`、逗号列表。受限 = 不是纯 `*`，
    用于 dom/dow 的"两者都受限时取 OR"语义。
    """
    field = field.strip()
    if not field:
        raise ValueError("cron 字段为空")
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            step = int(step_s)
            if step <= 0:
                raise ValueError(f"cron 步长必须为正数: {field}")
        if part == "*":
            start, end = low, high
        elif "-" in part:
            start_s, end_s = part.split("-", 1)
            start, end = int(start_s), int(end_s)
        else:
            start = end = int(part)
        if start < low or end > high or start > end:
            raise ValueError(f"cron 字段越界: {field}（允许 {low}-{high}）")
        values.update(range(start, end + 1, step))
    return values, field != "*"


class _CronSpec:
    def __init__(self, cron: str) -> None:
        parts = cron.split()
        if len(parts) != 5:
            raise ValueError(f"cron 必须是 5 段表达式: {cron!r}")
        self.minutes, _ = _expand_field(parts[0], 0, 59)
        self.hours, _ = _expand_field(parts[1], 0, 23)
        self.doms, self.dom_restricted = _expand_field(parts[2], 1, 31)
        self.months, _ = _expand_field(parts[3], 1, 12)
        dows, self.dow_restricted = _expand_field(parts[4], 0, 7)
        # cron 里 0 和 7 都是周日；内部统一成 0
        self.dows = {0 if d == 7 else d for d in dows}

    def day_matches(self, day: datetime) -> bool:
        dom_ok = day.day in self.doms
        dow_ok = ((day.weekday() + 1) % 7) in self.dows   # 周一=1 … 周日=0
        if self.dom_restricted and self.dow_restricted:
            return dom_ok or dow_ok
        if self.dom_restricted:
            return dom_ok
        if self.dow_restricted:
            return dow_ok
        return True


def next_run_utc_after(cron: str, timezone_name: str, after: datetime | None = None) -> datetime:
    """计算 cron 在指定时区中、严格晚于 `after` 的下一次触发时刻（返回 UTC datetime）。"""
    spec = _CronSpec(cron)
    try:
        tz = ZoneInfo(timezone_name)
    except Exception as exc:
        raise ValueError(f"未知时区: {timezone_name}") from exc

    now = after or datetime.now(timezone.utc)
    local = now.astimezone(tz)
    start = local.replace(second=0, microsecond=0) + timedelta(minutes=1)
    day = start.date()
    hours = sorted(spec.hours)
    minutes = sorted(spec.minutes)

    for _ in range(366 * 5):  # 最多向后找 5 年；找不到视为非法表达式
        if day.month in spec.months:
            probe = datetime(day.year, day.month, day.day, tzinfo=tz)
            if spec.day_matches(probe):
                for hour in hours:
                    for minute in minutes:
                        candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
                        if candidate >= start:
                            return candidate.astimezone(timezone.utc)
        day = day + timedelta(days=1)
    raise ValueError(f"cron 在 5 年内无触发时间: {cron!r}")


def _parse_schedule_at(value: str, timezone_name: str) -> datetime:
    """解析一次性任务的 `at`：带时区按其本身，裸时间按计划时区解释。"""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        try:
            dt = dt.replace(tzinfo=ZoneInfo(timezone_name))
        except Exception as exc:
            raise ValueError(f"未知时区: {timezone_name}") from exc
    return dt.astimezone(timezone.utc)


def _to_utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ----------------------------------------------------------------------------- Store
class Store:
    """SQLite 业务存储。每个方法一条短连接，可多线程调用。"""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    # ---------------------------------------------------------------- 连接 / 建表
    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_schema(self) -> None:
        """建表（幂等）。WAL 是数据库的持久属性，在这里写一次即可。"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA_SQL)
            # 老库补列：CREATE TABLE IF NOT EXISTS 不会加新列。
            columns = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
            if "last_seen" not in columns:
                conn.execute("ALTER TABLE sessions ADD COLUMN last_seen TEXT")

    def table_names(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        return [r["name"] for r in rows]

    def journal_mode(self) -> str:
        with self._connect() as conn:
            return str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()

    # ---------------------------------------------------------------- sessions
    def upsert_session(
        self,
        session_id: str,
        *,
        title: str,
        status: str,
        cwd: str | None = None,
        parent_id: str | None = None,
        pid: int | None = None,
        pid_started_at: str | None = None,
        first_message_at: str | None = None,
    ) -> None:
        """插入或更新会话。更新时不覆盖已有的 pid / parent / first_message_at（除非显式传入）。"""
        ts = now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions
                    (id, title, status, cwd, pid, pid_started_at, parent_id,
                     created_at, updated_at, first_message_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title,
                    status=excluded.status,
                    cwd=COALESCE(excluded.cwd, sessions.cwd),
                    pid=COALESCE(excluded.pid, sessions.pid),
                    pid_started_at=COALESCE(excluded.pid_started_at, sessions.pid_started_at),
                    parent_id=COALESCE(excluded.parent_id, sessions.parent_id),
                    updated_at=excluded.updated_at,
                    first_message_at=COALESCE(excluded.first_message_at, sessions.first_message_at)
                """,
                (
                    session_id,
                    title,
                    status,
                    cwd,
                    pid,
                    pid_started_at,
                    parent_id,
                    ts,
                    ts,
                    first_message_at,
                ),
            )

    def set_pid(self, session_id: str, pid: int, pid_started_at: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET pid=?, pid_started_at=?, updated_at=? WHERE id=?",
                (pid, pid_started_at, now_iso(), session_id),
            )

    def set_status(self, session_id: str, status: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET status=?, updated_at=? WHERE id=?",
                (status, now_iso(), session_id),
            )

    def set_first_message_at(self, session_id: str, ts: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET first_message_at=?, updated_at=? WHERE id=?",
                (ts or now_iso(), now_iso(), session_id),
            )

    def touch_session(self, session_id: str, ts: str | None = None) -> None:
        """worker 每轮模型调用的心跳（§5.7.3）；巡检用它判断停滞。"""
        now = now_iso()
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET last_seen=?, updated_at=? WHERE id=?",
                (ts or now, now, session_id),
            )

    def get_session(self, session_id: str) -> SessionRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return SessionRecord.from_row(row) if row else None

    def list_sessions(
        self,
        *,
        status: str | None = None,
        limit: int = 20,
        cursor: str | int | None = None,
    ) -> list[SessionRecord]:
        """按 `updated_at DESC, id DESC` 列会话。

        `cursor` 两种写法都接受：
        - 数字 → 当作 OFFSET；
        - 上次返回的最后一条 `id` → 作为锚点继续翻页（跨页时不重复）。
        """
        sql = "SELECT * FROM sessions"
        where: list[str] = []
        params: list[object] = []
        if status:
            where.append("status=?")
            params.append(status)
        if cursor not in (None, "", 0):
            cursor_s = str(cursor)
            if cursor_s.isdigit():
                if where:
                    sql += " WHERE " + " AND ".join(where)
                sql += " ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?"
                params.extend([limit, int(cursor_s)])
                with self._connect() as conn:
                    rows = conn.execute(sql, params).fetchall()
                return [SessionRecord.from_row(r) for r in rows]
            anchor = None
            with self._connect() as conn:
                anchor = conn.execute(
                    "SELECT updated_at FROM sessions WHERE id=?", (cursor_s,)
                ).fetchone()
            if anchor is not None:
                where.append("(updated_at < ? OR (updated_at = ? AND id < ?))")
                params.extend([anchor["updated_at"], anchor["updated_at"], cursor_s])
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [SessionRecord.from_row(r) for r in rows]

    # ---------------------------------------------------------------- runs
    def create_run(
        self,
        run_id: str,
        *,
        session_id: str,
        trigger: str,
        status: str,
        turn_id: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (id, session_id, trigger, status, started_at, turn_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, session_id, trigger, status, now_iso(), turn_id),
            )

    def finish_run(self, run_id: str, *, status: str, exit_code: int | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE runs SET status=?, ended_at=?, exit_code=? WHERE id=?",
                (status, now_iso(), exit_code, run_id),
            )

    # ---------------------------------------------------------------- schedules
    def create_schedule(
        self,
        *,
        cron: str | None = None,
        at: str | None = None,
        timezone: str = "Asia/Shanghai",
        prompt: str,
        misfire_grace_s: int = 300,
        catch_up: str = "once",
    ) -> ScheduleRecord:
        """创建调度。`at` 优先于 `cron`；两者必须给一个。

        `next_run_utc` 在这里统一算成 UTC（Z 结尾）；一次性任务的 `at` 原串也保存，
        便于排查"用户写的是什么时间"。
        """
        if not cron and not at:
            raise ValueError("create_schedule 需要 cron 或 at")
        if at:
            run_at = _parse_schedule_at(at, timezone)
            next_run_utc: str | None = _to_utc_iso(run_at)
        else:
            next_run_utc = _to_utc_iso(next_run_utc_after(str(cron), timezone))

        schedule_id = ulid()
        created_at = now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO schedules
                    (id, cron, at, timezone, prompt, status, next_run_utc,
                     last_run_utc, misfire_grace_s, catch_up, created_at)
                VALUES (?, ?, ?, ?, ?, 'active', ?, NULL, ?, ?, ?)
                """,
                (schedule_id, cron, at, timezone, prompt, next_run_utc, misfire_grace_s, catch_up, created_at),
            )
        return ScheduleRecord(
            id=schedule_id,
            cron=cron,
            at=at,
            timezone=timezone,
            prompt=prompt,
            status="active",
            next_run_utc=next_run_utc,
            last_run_utc=None,
            misfire_grace_s=misfire_grace_s,
            catch_up=catch_up,
            created_at=created_at,
        )

    def list_due_schedules(self, now_utc: str, limit: int = 20) -> list[ScheduleRecord]:
        """列出 `next_run_utc <= now_utc` 的 active 调度。

        SQL 先按同一 UTC 格式排序取候选，再用 `parse_iso` 精确比较（契约要求）。
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM schedules
                WHERE status='active' AND next_run_utc IS NOT NULL
                ORDER BY next_run_utc ASC
                LIMIT ?
                """,
                (max(limit * 10, limit),),
            ).fetchall()
        now_dt = parse_iso(now_utc)
        due: list[ScheduleRecord] = []
        for row in rows:
            if parse_iso(row["next_run_utc"]) <= now_dt:
                due.append(ScheduleRecord.from_row(row))
                if len(due) >= limit:
                    break
        return due

    def update_schedule_after_run(
        self,
        schedule_id: str,
        *,
        next_run_utc: str | None,
        ran_at_utc: str,
    ) -> None:
        """一次触发结束后更新状态：还有下次 → active；没有 → done（一次性任务）。"""
        status = "active" if next_run_utc else "done"
        with self._connect() as conn:
            conn.execute(
                "UPDATE schedules SET next_run_utc=?, last_run_utc=?, status=? WHERE id=?",
                (next_run_utc, ran_at_utc, status, schedule_id),
            )

    def set_schedule_status(self, schedule_id: str, status: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE schedules SET status=? WHERE id=?", (status, schedule_id))

    def get_schedule(self, schedule_id: str) -> ScheduleRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()
        return ScheduleRecord.from_row(row) if row else None

    def set_schedule_next_run(self, schedule_id: str, next_run_utc: str | None) -> None:
        """只改下一次触发时刻（恢复调度时重算 cron），不碰 `last_run_utc`。"""
        with self._connect() as conn:
            conn.execute("UPDATE schedules SET next_run_utc=? WHERE id=?", (next_run_utc, schedule_id))

    def list_schedules(self, *, status: str | None = None, limit: int = 50) -> list[ScheduleRecord]:
        sql = "SELECT * FROM schedules"
        params: list[object] = []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [ScheduleRecord.from_row(r) for r in rows]

    # ---------------------------------------------------------------- task_watch
    def mark_watch(
        self,
        task_id: str,
        reason: str,
        *,
        now: str | None = None,
    ) -> Literal["first", "seen"]:
        """记录一次巡检命中。

        返回 `"first"` 表示首次命中（或已 resolve 后再次命中），调用方应 XADD 提醒；
        返回 `"seen"` 表示同一 (task_id, reason) 的重复命中，抑制重复提醒。
        """
        ts = now or now_iso()
        dedupe_key = f"{task_id}:{reason}"
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status FROM task_watch WHERE task_id=? AND reason=?",
                (task_id, reason),
            ).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO task_watch
                        (task_id, reason, status, first_detected_at, last_detected_at,
                         alerted_at, dedupe_key)
                    VALUES (?, ?, 'active', ?, ?, ?, ?)
                    """,
                    (task_id, reason, ts, ts, ts, dedupe_key),
                )
                return "first"
            if str(row["status"]) != "active":
                # 已 resolve 后再次出现：重新打开并按首次处理，允许再次提醒用户
                conn.execute(
                    """
                    UPDATE task_watch
                    SET status='active', last_detected_at=?, alerted_at=?
                    WHERE task_id=? AND reason=?
                    """,
                    (ts, ts, task_id, reason),
                )
                return "first"
            conn.execute(
                "UPDATE task_watch SET last_detected_at=? WHERE task_id=? AND reason=?",
                (ts, task_id, reason),
            )
            return "seen"

    def resolve_watch(self, task_id: str, reason: str | None = None) -> None:
        with self._connect() as conn:
            if reason:
                conn.execute(
                    "UPDATE task_watch SET status='resolved' WHERE task_id=? AND reason=?",
                    (task_id, reason),
                )
            else:
                conn.execute("UPDATE task_watch SET status='resolved' WHERE task_id=?", (task_id,))

    # ---------------------------------------------------------------- 观测
    def record_tool_event(
        self,
        *,
        trace_id: str | None,
        session_id: str | None,
        tool: str,
        ok: bool,
        duration_ms: int,
        shell: str | None = None,
        error_class: str | None = None,
        fingerprint: str | None = None,
        retry_count: int = 0,
        exit_code: int | None = None,
        args_hash: str | None = None,
    ) -> None:
        """worker 也可调用（每次短连接）；只追加，不改其它表。"""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO tool_events
                    (id, trace_id, session_id, tool, shell, error_class, fingerprint,
                     retry_count, exit_code, ok, duration_ms, args_hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ulid(),
                    trace_id,
                    session_id,
                    tool,
                    shell,
                    error_class,
                    fingerprint,
                    retry_count,
                    exit_code,
                    1 if ok else 0,
                    int(duration_ms),
                    args_hash,
                    now_iso(),
                ),
            )

    def record_cost(
        self,
        *,
        trace_id: str | None,
        scope: str,
        model: str,
        input_tokens: int,
        cached_tokens: int,
        output_tokens: int,
        cost_usd: float,
        price_version: str = "v1",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO cost_ledger
                    (id, ts_utc, trace_id, scope, model, input_tokens, cached_tokens,
                     output_tokens, cost_usd, price_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ulid(),
                    now_utc_iso(),
                    trace_id,
                    scope,
                    model,
                    int(input_tokens),
                    int(cached_tokens),
                    int(output_tokens),
                    float(cost_usd),
                    price_version,
                ),
            )

    # ---------------------------------------------------------------- 审批
    def create_pending_approval(
        self,
        *,
        token: str,
        trace_id: str | None,
        tool: str,
        args_json: str,
        ttl_s: int = 600,
    ) -> None:
        created = datetime.now(timezone.utc)
        expires = created + timedelta(seconds=int(ttl_s))
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pending_approvals
                    (token, trace_id, tool, args_json, created_at, expires_at, status)
                VALUES (?, ?, ?, ?, ?, ?, 'pending')
                """,
                (
                    token,
                    trace_id,
                    tool,
                    args_json,
                    created.isoformat(timespec="milliseconds"),
                    expires.isoformat(timespec="milliseconds"),
                ),
            )

    def decide_approval(self, token: str, decision: str, note: str = "") -> dict | None:
        """裁决审批：只有仍为 pending 且未过期的 token 才能生效。

        返回审批内容（供调用方执行已批准的工具）；不存在/已处理/已过期返回 None。
        """
        now_dt = datetime.now(timezone.utc)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM pending_approvals WHERE token=?", (token,)).fetchone()
            if row is None or str(row["status"]) != "pending":
                return None
            expires_at = parse_iso(str(row["expires_at"]))
            if expires_at <= now_dt:
                conn.execute(
                    "UPDATE pending_approvals SET status='expired' WHERE token=?", (token,)
                )
                return None

            decided_at = now_iso()
            args_json = str(row["args_json"])
            args_hash = hashlib.sha256(args_json.encode("utf-8")).hexdigest()[:16]
            conn.execute(
                "UPDATE pending_approvals SET status=? WHERE token=?", (decision, token)
            )
            conn.execute(
                """
                INSERT INTO approval_log
                    (id, trace_id, tool, args_hash, args_json, decision, decided_at, note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ulid(),
                    row["trace_id"],
                    row["tool"],
                    args_hash,
                    args_json,
                    decision,
                    decided_at,
                    note,
                ),
            )
            return {
                "token": token,
                "trace_id": row["trace_id"],
                "tool": row["tool"],
                "args_json": args_json,
                "decision": decision,
                "note": note,
                "decided_at": decided_at,
            }
