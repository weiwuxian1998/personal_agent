"""契约自检：不依赖 Redis/Postgres/飞书，验证冻结文件的行为。

用法：python scripts/check_contracts.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.vision import adapt_content, adapt_messages  # noqa: E402
from bus.envelope import event_from_redis, event_to_redis, new_event, render_digest  # noqa: E402
from contracts import EVENT_SOURCES, Event, IncomingMessage, SendResult, TaskSpec, parse_iso, ulid  # noqa: E402


def main() -> int:
    failures: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail and not cond else ""))
        if not cond:
            failures.append(name)

    ids = [ulid() for _ in range(1000)]
    check("ulid_unique", len(set(ids)) == 1000)
    check("ulid_len", all(len(i) == 26 for i in ids))

    e = new_event("user_message", "feishu", {"text": "你好", "chat_id": "oc_x", "message_id": "om_1"})
    raw = event_to_redis(e)
    e2 = event_from_redis(raw)
    check("event_roundtrip", e2.to_dict() == e.to_dict())
    check("event_trace_default", e.trace_id == e.id)

    digest = render_digest(
        [
            e,
            new_event("worker_result", "worker", {"task_id": "worker:01ABC", "text": "完成了"}),
            new_event("timer", "scheduler", {"schedule_id": "s1", "prompt": "该喝水了"}),
        ]
    )
    check("digest_has_user", "用户" in digest)
    check("digest_has_worker", "完成任务" in digest or "完成" in digest)
    check("digest_has_reply_hint", "ReplyUser" in digest)

    try:
        Event(type="unknown", source="system", payload={})
    except ValueError:
        check("event_rejects_unknown_type", True)
    else:
        check("event_rejects_unknown_type", False)

    check("parse_iso_utc", parse_iso("2026-11-08T12:00:00Z").hour == 12)
    check("task_roundtrip", TaskSpec.from_json(TaskSpec(task_id=ulid(), prompt="p", title="t",
                                                         trace_id="tr", cwd="C:/x",
                                                         workspace_root="C:/w").to_json()).task_id != "")

    im = IncomingMessage(channel="console", chat_id="console", chat_type="p2p",
                         message_id="m1", event_id="e1", sender_id="u1",
                         text="hi", create_time=0)
    check("incoming_message", im.text == "hi")
    check("send_result", SendResult(ok=True, message_id="om_2").ok)

    blocks = [
        {"type": "text", "text": "[图片] C:/w/a.png"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
    ]
    check("vision_true_keeps_blocks", adapt_content(blocks, supports_vision=True) == blocks)
    degraded = adapt_content(blocks, supports_vision=False)
    check("vision_false_removes_image", all(b.get("type") != "image_url" for b in degraded))
    check("vision_false_keeps_text", degraded[0] == blocks[0])
    check("vision_false_adds_placeholder", "不支持视觉" in degraded[1]["text"])

    class _Msg:
        def __init__(self, content):
            self.content = content

        def model_copy(self, update):
            return _Msg(update.get("content", self.content))

    original = [_Msg(blocks)]
    adapted = adapt_messages(original, supports_vision=False)
    check("adapt_messages_no_mutation", original[0].content == blocks)
    check("adapt_messages_new_content", "image_url" not in [b.get("type") for b in adapted[0].content])

    # 渠道工厂：console 模式不得把 lark_oapi 拖进导入（实测导入 47s）
    from bus.stream import Bus  # noqa: E402
    from channel.factory import build_channel  # noqa: E402
    from config import settings  # noqa: E402

    settings.channel_kind = "console"
    channel = build_channel(settings)
    check("channel_factory_console", channel.name == "console")
    check("channel_factory_lazy_lark", "lark_oapi" not in sys.modules)
    check("bus_lag_exists", callable(getattr(Bus, "lag", None)))
    check("event_source_console", "console" in EVENT_SOURCES)

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
