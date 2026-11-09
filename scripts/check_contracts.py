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

    # ------------------------------------------------ lark 惰性导入（子进程实测）
    # 同进程的 sys.modules 断言可能被前面的 import 污染，这里用全新解释器验证
    import subprocess  # noqa: E402

    probe = subprocess.run(
        [sys.executable, "-c", "import sys, channel.factory; print('lark_oapi' in sys.modules)"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=180,
    )
    check("channel_factory_lazy_lark_subprocess", probe.stdout.strip() == "False", probe.stdout + probe.stderr)

    # ------------------------------------------------ Bus.lag 行为（真实 Redis）
    import redis as _redis  # noqa: E402
    import uuid as _uuid  # noqa: E402

    client = _redis.from_url(settings.redis_url, decode_responses=True, socket_timeout=5)
    try:
        client.ping()
    except Exception as exc:  # noqa: BLE001
        check("redis_available", False, str(exc)[:120])
    else:
        name = f"asst:test:contract:{_uuid.uuid4().hex[:8]}"
        b = Bus(client)
        b.stream, b.group = name, "g"
        try:
            client.xgroup_create(name, "g", id="0", mkstream=True)
            b.xadd(new_event("user_message", "console", {"text": "x"}), stream=name, maxlen=100)
            check("lag_undelivered_positive", b.lag() == 1, f"lag={b.lag()}")
            entries = b.read_batch(block_ms=100, count=10)
            b.ack(entries)
            # 关键断言：XACK 后条目仍留在 stream（xlen>0），但 lag 必须归零。
            # 任何把判断改回 XLEN 的改动都会让这条失败。
            check("lag_after_ack_zero_not_xlen", b.lag() == 0 and b.xlen() > 0,
                  f"lag={b.lag()} xlen={b.xlen()}")
        finally:
            client.delete(name)

        # 被 MAXLEN 裁剪后 Redis 返回 lag=None；此时必须仍能识别「有未投递事件」，
        # 否则 supervisor 永远不拉 router（已实测 180s 无人消费）。
        trimmed = f"asst:test:contract:{_uuid.uuid4().hex[:8]}"
        b2 = Bus(client)
        b2.stream, b2.group = trimmed, "g"
        try:
            client.xgroup_create(trimmed, "g", id="0", mkstream=True)
            for _ in range(5):
                b2.xadd(new_event("user_message", "console", {"text": "x"}), stream=trimmed, maxlen=1)
            raw = client.xinfo_groups(trimmed)[0]
            check("lag_none_fallback_positive", b2.lag() > 0,
                  f"raw_lag={raw.get('lag')} computed={b2.lag()}")
        finally:
            client.delete(trimmed)

    # ------------------------------------------------ Thread 子类不得遮蔽标准库属性
    import threading  # noqa: E402

    import main_gateway  # noqa: E402
    import main_router  # noqa: E402

    thread_classes = [
        obj for module in (main_gateway, main_router)
        for obj in vars(module).values()
        if isinstance(obj, type) and issubclass(obj, threading.Thread) and obj.__module__ == module.__name__
    ]
    check("thread_classes_found", len(thread_classes) >= 2, f"found={[c.__name__ for c in thread_classes]}")
    # 只查 Thread 的**私有**属性（`_stop` / `_tstate_lock` / `_started` 等）；
    # `run` / `__init__` 是本就要覆盖的公开接口，双下划线是解释器保留名。
    inherited_private = {
        name for name in dir(threading.Thread) if name.startswith("_") and not name.startswith("__")
    }
    check("thread_private_attrs_probe", "_stop" in inherited_private)
    for cls in thread_classes:
        clash = set(cls.__dict__) & inherited_private
        check(f"thread_no_stdlib_shadow:{cls.__name__}", not clash, f"clash={sorted(clash)}")

    class _FakeRedis:
        def set(self, *args, **kwargs):
            return True

    hb = main_router.Heartbeat(_FakeRedis(), 10)
    hb.start()
    try:
        hb.stop()  # 内部 join(timeout=2)；若遮蔽 Thread._stop 会抛 TypeError
        check("heartbeat_join_ok", True)
    except TypeError as exc:
        check("heartbeat_join_ok", False, str(exc))

    # ------------------------------------------------ Vision 中间件：checkpoint 保留原图
    from langchain.agents import create_agent  # noqa: E402
    from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
    from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402
    from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402
    from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402

    received: list = []

    class _EchoModel(BaseChatModel):
        @property
        def _llm_type(self) -> str:
            return "echo"

        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            received.append(list(messages))
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="ok"))])

    from agents.middleware import VisionAdapterMiddleware  # noqa: E402

    settings.llm_supports_vision = False
    graph = create_agent(
        model=_EchoModel(), tools=[], system_prompt="s",
        middleware=[VisionAdapterMiddleware(settings)],
        checkpointer=InMemorySaver(), name="vision-check",
    )
    cfg = {"configurable": {"thread_id": "vision-check"}}
    image_block = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}
    graph.invoke({"messages": [HumanMessage(content=[{"type": "text", "text": "看图"}, image_block])]}, cfg)

    saved = graph.get_state(cfg).values["messages"]
    saved_human = [m for m in saved if isinstance(m, HumanMessage)][0]
    saved_types = [b.get("type") for b in saved_human.content]
    check("vision_checkpoint_keeps_image", "image_url" in saved_types, f"saved_blocks={saved_types}")

    sent_types = [b.get("type") for m in received[0] for b in (m.content if isinstance(m.content, list) else [])]
    check("vision_request_downgraded", "image_url" not in sent_types, f"sent_blocks={sent_types}")

    # ------------------------------------------------ ToolContextMiddleware：落盘路径带真实 tool_call_id
    # §7.7 要求落盘到 `tool_outputs/<trace_id>/<tool_call_id>-<n>.md`。若装配层不赋值，
    # `current_tool_call_id` 为空 → 文件名退化为随机 ULID、目录退化为 `notrace/`，
    # trace 与落盘文件无法关联。这里用**真实 graph + 真实 tools/web.py**跑一遍，
    # 只把 HTTP 层换成大正文桩（不联网），断言文件名里确实是模型给出的 tool_call id。
    import tempfile  # noqa: E402

    import tools.web as web_mod  # noqa: E402
    from agents.context import current_trace_id  # noqa: E402
    from agents.middleware import ToolContextMiddleware  # noqa: E402
    from tools.web import build_web_tools  # noqa: E402

    REAL_CALL_ID = "call-real-001"
    BIG_CONTENT = "x" * 100_000  # > tool_output_max_bytes(64 KiB)，强制落盘

    def _run_fetch_graph(*, middleware: list, outputs_dir: Path) -> list[str]:
        calls = {"n": 0}

        class _ToolCallModel(BaseChatModel):
            @property
            def _llm_type(self) -> str:
                return "toolcall"

            def bind_tools(self, tools, **kwargs):
                return self

            def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    msg = AIMessage(
                        content="",
                        tool_calls=[{
                            "name": "WebFetch",
                            "args": {"url": "https://m.example/x"},
                            "id": REAL_CALL_ID,
                            "type": "tool_call",
                        }],
                    )
                else:
                    msg = AIMessage(content="ok")
                return ChatResult(generations=[ChatGeneration(message=msg)])

        tools = build_web_tools(settings=settings, tool_outputs_dir=outputs_dir)
        g = create_agent(
            model=_ToolCallModel(), tools=tools, system_prompt="s",
            middleware=middleware, checkpointer=InMemorySaver(), name="toolctx",
        )
        # trace_id 在真实链路里由 router/worker 进程设置；这里手工置位，
        # 断言重点在 tool_call_id 是否被装配层赋值。
        current_trace_id.set("tr-assert")
        g.invoke(
            {"messages": [HumanMessage(content="抓取")]},
            {"configurable": {"thread_id": "toolctx"}},
        )
        return sorted(str(p.relative_to(outputs_dir)) for p in Path(outputs_dir).rglob("*.md"))

    _orig_call_json = web_mod._call_json
    _orig_search_key = settings.deepseek_search_api_key
    web_mod._call_json = lambda *a, **k: {"ok": True, "data": {"title": "T", "content": BIG_CONTENT}}
    settings.deepseek_search_api_key = "sk-unit-test-not-a-real-key"
    try:
        with tempfile.TemporaryDirectory() as td:
            on = _run_fetch_graph(
                middleware=[VisionAdapterMiddleware(settings), ToolContextMiddleware()],
                outputs_dir=Path(td),
            )
            check(
                "tool_artifact_path_uses_tool_call_id",
                len(on) == 1
                and REAL_CALL_ID in Path(on[0]).name
                and Path(on[0]).parent.name == "tr-assert",
                f"files={on}",
            )
        with tempfile.TemporaryDirectory() as td:
            off = _run_fetch_graph(
                middleware=[VisionAdapterMiddleware(settings)],  # 故意不装 ToolContext
                outputs_dir=Path(td),
            )
            check(
                "tool_artifact_path_without_middleware_loses_id",
                len(off) == 1 and REAL_CALL_ID not in Path(off[0]).name,
                f"files={off}",
            )
    finally:
        web_mod._call_json = _orig_call_json
        settings.deepseek_search_api_key = _orig_search_key

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
