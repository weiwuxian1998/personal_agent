"""protocol_adapter 验收脚本（退出码 0 才算交付）。

完全只读验收：所有 REST/WS 断言都打在真实 Redis/SQLite/Postgres 上，但不写任何
业务数据；并断言写入口（POST /api/messages、WS send 帧）已不存在/被忽略。

运行：python scripts/check_adapter.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

import protocol_adapter as pa  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def test_readonly_api() -> None:
    print("[只读 REST / WS（真实数据）]")
    app = pa.create_app()
    with TestClient(app) as client:
        r = client.get("/api/status")
        status = r.json()
        check("status_200", r.status_code == 200)
        check("status_redis_ok", status.get("redis_ok") is True)
        check("status_shape", all(k in status for k in ("gateway", "router", "outbox", "counts")))
        check("status_router_stream_metrics", "lag" in status["router"]["stream"])
        print(f"       router.alive={status['router']['alive']} "
              f"gateway.alive={status['gateway']['alive']} "
              f"postgres_ok={status['postgres_ok']}")

        r = client.get("/api/sessions?limit=5")
        sessions = r.json()
        check("sessions_ok", sessions.get("ok") is True and sessions.get("total", 0) > 0)
        first = sessions["items"][0] if sessions.get("items") else None
        check("sessions_pid_alive_field", first is not None and "pid_alive" in first)

        if first:
            r = client.get(f"/api/sessions/{first['id']}?limit=5")
            detail = r.json()
            check("session_detail_ok", detail.get("ok") is True)
            check("session_detail_context", "context" in detail and "messages" in detail["context"])
            r = client.get(f"/api/sessions/{first['id']}/log?bytes=4096")
            check("session_log_endpoint", r.status_code == 200)

        r = client.get("/api/router/context?limit=10")
        ctx = r.json()
        check("router_context_ok", ctx.get("ok") is True)
        if ctx.get("source") == "checkpoint":
            check("router_context_messages", ctx.get("total", 0) > 0)
            check("router_context_view_shape",
                  all("role" in m and "content" in m for m in ctx["messages"]))
            check("router_context_pagination", "next_cursor" in ctx)
        else:
            print(f"       SKIP(router checkpoint): {ctx.get('note')}")

        r = client.get("/api/turns?limit=5")
        turns = r.json()
        check("turns_ok", turns.get("ok") is True)
        check("turns_shape", all("digest" in t for t in turns.get("items", [])))

        r = client.get("/api/inbox?limit=5")
        check("inbox_ok", r.json().get("ok") is True)

        r = client.get("/api/schedules")
        schedules = r.json()
        check("schedules_ok", schedules.get("ok") is True)
        check("schedules_fields",
              all("next_run_utc" in s and "status" in s for s in schedules.get("items", [])))

        r = client.get("/api/events?stream=router&count=5")
        events = r.json()
        check("events_ok", events.get("ok") is True)
        check("events_envelope",
              all("type" in e["event"] and "payload" in e["event"] for e in events.get("items", [])))

        r = client.get("/api/watches")
        check("watches_ok", r.json().get("ok") is True)

        # 写入口必须已拆除：POST /api/messages 不存在（404/405）
        r = client.post("/api/messages", json={"text": "只读验收，不应入队"})
        check("messages_route_removed", r.status_code in (404, 405), f"got {r.status_code}")

        with client.websocket_connect("/ws") as ws:
            hello = ws.receive_json()
            check("ws_hello", hello.get("type") == "hello" and "status" in hello)
            check("ws_hello_replay", isinstance(hello.get("events"), list))
            # 旧版 send 帧现在必须被忽略（不产生任何写入），ping 仍正常回应
            ws.send_json({"type": "send", "text": "只读验收，不应入队"})
            ws.send_json({"type": "ping"})
            pong = None
            for _ in range(10):
                frame = ws.receive_json()
                if frame.get("type") == "pong":
                    pong = frame
                    break
                check("ws_no_send_result", frame.get("type") != "send_result", str(frame))
            check("ws_ping_pong", pong is not None)


def main() -> int:
    test_readonly_api()
    print(f"\n{PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())