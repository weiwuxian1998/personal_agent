"""Schedule：定时任务的唯一管理入口（契约 §7.5 B1 / §9.3）。

边界：
- 本文件只读写 `schedules` 表，**不执行任何任务、不 import LLM**；
- 真正的触发由 gateway 的 `ScheduleScanner` 完成：扫描 `next_run_utc <= now` 的行，
  发一条 `timer` 事件唤醒 router（payload 固定 `schedule_id/scheduled_at/prompt`），
  然后推进 `next_run_utc`；一次性任务没有下一次 → `status="done"`；
- 时间统一存 UTC（`next_run_utc`）；`cron` / `at` 按各自的 `timezone` 解释。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from langchain_core.tools import tool

from contracts import ScheduleRecord
from store.sqlite import Store, next_run_utc_after

# 工具函数里有个名为 timezone 的参数，会遮蔽模块级 `timezone`，这里先取出来备用。
UTC = timezone.utc


def utc_iso(dt: datetime) -> str:
    """UTC ISO8601（Z 结尾、毫秒精度），与 `contracts.now_utc_iso` 同格式。"""
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def next_run_iso(record: ScheduleRecord, *, after: datetime) -> str | None:
    """算这条调度的下一次触发时刻；一次性任务（只有 `at`）返回 None。"""
    if not record.cron:
        return None
    return utc_iso(next_run_utc_after(record.cron, record.timezone, after=after))


def _row(record: ScheduleRecord) -> dict[str, Any]:
    return {
        "schedule_id": record.id,
        "cron": record.cron,
        "at": record.at,
        "timezone": record.timezone,
        "status": record.status,
        "next_run_utc": record.next_run_utc,
        "last_run_utc": record.last_run_utc,
        "prompt": record.prompt[:200],
    }


def build_schedule_tools(*, store: Store, timezone: str = "Asia/Shanghai") -> list[Any]:
    """构造 `Schedule` 工具（router 用；契约 §7.5 B1）。"""
    default_timezone = timezone or "Asia/Shanghai"

    @tool
    def Schedule(
        action: str,
        cron: str | None = None,
        at: str | None = None,
        timezone: str = default_timezone,
        prompt: str = "",
        misfire_grace_s: int = 300,
        schedule_id: str | None = None,
    ) -> dict:
        """管理定时任务。触发时会唤醒你并把 `prompt` 原样告诉你，由你决定做什么。

        Args:
            action: create / list / cancel / pause / resume。
            cron: 5 段 cron（分 时 日 月 周），如 "0 9 * * *" 表示每天 9:00。
            at: 一次性任务的时刻（ISO8601）；裸时间按 timezone 解释。与 cron 二选一。
            timezone: cron / at 使用的时区，默认 Asia/Shanghai。
            prompt: 到点后要交代给模型的任务描述（create 必填）。
            misfire_grace_s: 超过该秒数仍未触发的视为错过（按 catch_up 处理）。
            schedule_id: cancel / pause / resume 的目标；list 可省略。
        """
        name = (action or "").strip().lower()
        tz = (timezone or default_timezone).strip() or default_timezone

        if name == "create":
            body = (prompt or "").strip()
            if not body:
                return {"ok": False, "error_class": "missing_prompt", "message": "create 需要非空 prompt"}
            if not cron and not at:
                return {"ok": False, "error_class": "missing_trigger", "message": "create 需要 cron 或 at"}
            try:
                record = store.create_schedule(
                    cron=(cron or None),
                    at=(at or None),
                    timezone=tz,
                    prompt=body,
                    misfire_grace_s=int(misfire_grace_s),
                )
            except ValueError as exc:
                return {
                    "ok": False,
                    "error_class": "invalid_cron" if cron else "invalid_at",
                    "message": str(exc),
                }
            result = _row(record)
            result["ok"] = True
            if at and record.next_run_utc and record.next_run_utc <= utc_iso(datetime.now(UTC)):
                result["note"] = "at 已过期，会在下一次扫描时立即触发一次，随后 status=done"
            return result

        if name == "list":
            records = store.list_schedules(limit=50)
            return {"ok": True, "count": len(records), "schedules": [_row(r) for r in records]}

        if name in ("cancel", "pause", "resume"):
            target = (schedule_id or "").strip()
            if not target:
                return {
                    "ok": False,
                    "error_class": "missing_schedule_id",
                    "message": f"{name} 需要 schedule_id（先用 action=list 查）",
                }
            record = store.get_schedule(target)
            if record is None:
                return {"ok": False, "error_class": "not_found", "message": f"没有该调度: {target}"}

            if name == "cancel":
                store.set_schedule_status(target, "cancelled")
                return {"ok": True, **_row(record), "status": "cancelled"}
            if name == "pause":
                if record.status != "active":
                    return {"ok": True, **_row(record), "note": f"当前状态是 {record.status}，未改动"}
                store.set_schedule_status(target, "paused")
                return {"ok": True, **_row(record), "status": "paused"}

            # resume：cron 从"现在"重新算下一次，避免补跑一堆历史时刻
            if record.status == "active":
                return {"ok": True, **_row(record), "note": "本来就是 active"}
            next_iso = next_run_iso(record, after=datetime.now(UTC))
            store.set_schedule_next_run(target, next_iso)
            store.set_schedule_status(target, "active")
            return {
                "ok": True,
                **_row(record),
                "status": "active",
                "next_run_utc": next_iso,
                "note": "cron 已按当前时间重算下一次；一次性任务保留原 at" if record.cron else "一次性任务保留原 at",
            }

        return {
            "ok": False,
            "error_class": "invalid_action",
            "message": f"未知 action: {action!r}；可用 create/list/cancel/pause/resume",
        }

    return [Schedule]
