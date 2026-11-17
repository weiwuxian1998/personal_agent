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

    # ------------------------------------------------ router 启动路径不得依赖渠道层
    # 旧实现里 router 调 build_channel() → import lark_oapi，冷启动被拖到 58–62s。
    # 这里用全新解释器模拟 router 启动（只 import 模块，不执行 main）。
    router_probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, main_router; "
            "print('lark_oapi' in sys.modules); "
            "print('CHANNEL:' + ','.join(sorted(m for m in sys.modules if m == 'channel' or m.startswith('channel.'))))",
        ],
        cwd=str(ROOT), capture_output=True, text=True, timeout=300,
    )
    router_lines = [line.strip() for line in router_probe.stdout.strip().splitlines()]
    check(
        "router_startup_no_lark_subprocess",
        router_lines[:1] == ["False"],
        router_probe.stdout + router_probe.stderr,
    )
    check(
        "router_import_graph_no_channel",
        len(router_lines) > 1 and router_lines[1] == "CHANNEL:",
        router_probe.stdout + router_probe.stderr,
    )

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

    # ------------------------------------------------ 思考模式开关的接线（真实效果靠 §14-38 的 reasoning_tokens 实测）
    from agents.base import build_chat_model  # noqa: E402

    _orig_thinking_disabled = settings.llm_thinking_disabled
    try:
        settings.llm_thinking_disabled = True
        thinking_off = build_chat_model(settings)
        check(
            "thinking_switch_off_extra_body",
            getattr(thinking_off, "extra_body", None) == {"thinking": {"type": "disabled"}},
            f"extra_body={getattr(thinking_off, 'extra_body', None)}",
        )
        settings.llm_thinking_disabled = False
        thinking_on = build_chat_model(settings)
        thinking_on_body = getattr(thinking_on, "extra_body", None) or {}
        check(
            # 思考开启时不得下发 `thinking` 字段；`reasoning_effort` 是低思考档位的
            # 显式配置（.env LLM_REASONING_EFFORT，默认 low），允许出现。
            "thinking_switch_on_no_extra_body",
            "thinking" not in thinking_on_body,
            f"extra_body={thinking_on_body}",
        )
    finally:
        settings.llm_thinking_disabled = _orig_thinking_disabled

    # ------------------------------------------------ worker 工具输出瘦身（§7.7）
    import json  # noqa: E402
    import tempfile as _tempfile  # noqa: E402

    check(
        "worker_defaults_web_content_2kb",
        settings.worker_web_default_count == 3 and settings.worker_web_content_chars == 2048,
        f"count={settings.worker_web_default_count} chars={settings.worker_web_content_chars}",
    )

    # 工具输出瘦身：worker 默认 count=3 / need_content=false，正文硬截断 2 KB
    _orig_call_json2 = web_mod._call_json
    _orig_search_key2 = settings.deepseek_search_api_key
    sent_payloads: list[dict] = []

    def _slim_call_json(url, payload, *, api_key, timeout_s):
        sent_payloads.append(dict(payload))
        if "/fetch" in str(url):
            return {"ok": True, "data": {"title": "T", "content": "c" * 9000}}
        return {
            "ok": True,
            "data": {"results": [{"title": "T", "url": "https://e/x", "content": "y" * 9000}]},
        }

    web_mod._call_json = _slim_call_json
    settings.deepseek_search_api_key = "sk-unit-test-not-a-real-key"
    try:
        with _tempfile.TemporaryDirectory() as td:
            router_tools = build_web_tools(settings=settings, tool_outputs_dir=Path(td))
            worker_tools = build_web_tools(
                settings=settings,
                tool_outputs_dir=Path(td),
                default_count=settings.worker_web_default_count,
                default_need_content=False,
                max_content_chars=settings.worker_web_content_chars,
            )
            router_tools[0].invoke({"query": "router 默认"})
            worker_search = worker_tools[0].invoke({"query": "worker 默认"})
            worker_fetch = worker_tools[1].invoke({"url": "https://e/x"})
    finally:
        web_mod._call_json = _orig_call_json2
        settings.deepseek_search_api_key = _orig_search_key2

    check(
        "web_router_default_unchanged",
        sent_payloads[0] == {"query": "router 默认", "count": 5, "need_content": False},
        str(sent_payloads[0]),
    )
    check(
        "web_worker_default_count3_no_content",
        sent_payloads[1] == {"query": "worker 默认", "count": 3, "need_content": False},
        str(sent_payloads[1]),
    )
    worker_content = worker_search["results"][0]["content"]
    check(
        "web_worker_search_content_capped_2kb",
        worker_search["count"] == 3
        and worker_search["need_content"] is False
        and worker_search["results"][0].get("content_truncated") is True
        and "正文已截断" in worker_content
        and len(worker_content) <= settings.worker_web_content_chars + 40,
        f"len={len(worker_content)}",
    )
    fetch_content = worker_fetch["content"]
    check(
        "web_worker_fetch_content_capped_2kb",
        worker_fetch.get("truncated") is True
        and worker_fetch.get("content_truncated") is True
        and "正文已截断" in fetch_content
        and len(fetch_content) <= settings.worker_web_content_chars + 40,
        f"len={len(fetch_content)}",
    )

    # ------------------------------------------------ 失败原因：必须带异常类名（§9）
    import worker_runner  # noqa: E402
    from langgraph.errors import GraphRecursionError  # noqa: E402

    recursion_exc = GraphRecursionError(
        "Recursion limit of 40 reached without hitting a stop condition.\n\n"
        "For troubleshooting, visit: https://docs.langchain.com/oss/python/langgraph/errors/GRAPH_RECURSION_LIMIT"
    )
    summary = worker_runner._error_summary(recursion_exc)
    check(
        "error_summary_starts_with_class_name",
        summary.startswith("GraphRecursionError: "),
        summary[:80],
    )
    check(
        "error_summary_keeps_message_not_last_traceback_line",
        "Recursion limit of 40" in summary and summary.rstrip().endswith("GRAPH_RECURSION_LIMIT"),
        summary[:120],
    )
    with _tempfile.TemporaryDirectory() as td:
        worker_runner._write_result(
            Path(td), "01TASKERROR", status="error", text="任务执行失败", error=summary,
        )
        saved = json.loads(
            (Path(td) / "01TASKERROR" / "result.json").read_text(encoding="utf-8")
        )
        check(
            "result_json_error_contains_class_name",
            "GraphRecursionError" in saved.get("error", ""),
            str(saved)[:120],
        )

    # ------------------------------------------------ chat_id 贯穿：task.json → worker_result → router
    import tools.sessions as sessions_mod  # noqa: E402
    from agents.context import current_chat_id  # noqa: E402
    from store.sqlite import Store  # noqa: E402
    from tools.sessions import SessionService  # noqa: E402

    class _FakeProc:
        pid = 4242

    class _CaptureBus:
        def __init__(self, client=None):
            self.events: list = []

        def xadd(self, event, stream=None, maxlen=None):
            self.events.append(event)

    with _tempfile.TemporaryDirectory() as td:
        tmp_settings = settings.model_copy(
            update={"data_dir": Path(td) / "data", "workspace_root": Path(td) / "ws"}
        )
        for path in (
            tmp_settings.workers_dir,
            tmp_settings.logs_dir,
            tmp_settings.db_path.parent,
            tmp_settings.workspace_root,
        ):
            path.mkdir(parents=True, exist_ok=True)
        store = Store(tmp_settings.db_path)
        store.init_schema()
        svc = SessionService(
            store=store, bus=None, redis_client=None, settings=tmp_settings, repo_root=ROOT,
        )

        _orig_popen = sessions_mod.subprocess.Popen
        sessions_mod.subprocess.Popen = lambda *a, **k: _FakeProc()  # type: ignore[assignment]
        try:
            dispatched: dict[str, dict] = {}

            def _dispatch(tag: str, chat_id: str) -> None:
                token = current_chat_id.set(chat_id)
                try:
                    dispatched[tag] = svc.create_session(f"并发任务 {tag}")
                finally:
                    current_chat_id.reset(token)

            threads = [
                threading.Thread(target=_dispatch, args=("A", "oc_A")),
                threading.Thread(target=_dispatch, args=("B", "oc_B")),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            sessions_mod.subprocess.Popen = _orig_popen  # type: ignore[assignment]

        tasks = {}
        for tag in ("A", "B"):
            task_path = tmp_settings.workers_dir / dispatched[tag]["task_id"] / "task.json"
            tasks[tag] = TaskSpec.from_json(task_path.read_text(encoding="utf-8"))
        check(
            "task_json_carries_chat_id",
            tasks["A"].chat_id == "oc_A" and tasks["B"].chat_id == "oc_B",
            f"A={tasks['A'].chat_id} B={tasks['B'].chat_id}",
        )

        bus = _CaptureBus()
        _orig_worker_bus = worker_runner.Bus
        worker_runner.Bus = lambda client: bus  # type: ignore[assignment]
        try:
            for tag in ("A", "B"):
                worker_runner._publish(None, tasks[tag], text="完成", status="done")
        finally:
            worker_runner.Bus = _orig_worker_bus  # type: ignore[assignment]
        check(
            "worker_result_carries_chat_id",
            [ev.payload.get("chat_id") for ev in bus.events] == ["oc_A", "oc_B"],
            str([ev.payload.get("chat_id") for ev in bus.events]),
        )
        routed = {
            tag: main_router._latest_user_chat([ev])[0]
            for tag, ev in zip(("A", "B"), bus.events)
        }
        check(
            "worker_result_routes_back_to_own_chat",
            routed == {"A": "oc_A", "B": "oc_B"},
            str(routed),
        )

    # ------------------------------------------------ 上下文预算与折叠（§3.6 / §6.4）
    from langchain_core.messages import (  # noqa: E402
        AIMessage,
        HumanMessage,
        RemoveMessage,
        ToolMessage,
    )

    from agents.context_budget import compute_cut, estimate_tokens  # noqa: E402
    from agents.middleware import ContextBudgetMiddleware  # noqa: E402

    def _msg_chain() -> list:
        chain: list = []
        for i in range(1, 5):
            chain.append(HumanMessage(content=f"用户消息 {i}", id=f"h{i}"))
            chain.append(
                AIMessage(
                    content="",
                    tool_calls=[{"name": "Shell", "args": {"command": "dir"}, "id": f"c{i}"}],
                    id=f"a{i}",
                )
            )
            chain.append(ToolMessage(content="输出" * 500, tool_call_id=f"c{i}", id=f"t{i}"))
        return chain

    chain = _msg_chain()
    check("context_token_estimate_positive", estimate_tokens(chain) > 0, str(estimate_tokens(chain)))
    cut = compute_cut(chain, keep_recent=2, keep_recent_tokens=10_000)
    last_human = max(i for i, m in enumerate(chain) if isinstance(m, HumanMessage))
    check("context_fold_keeps_last_human", 0 < cut <= last_human, f"cut={cut} last_human={last_human}")

    kept = chain[cut:]
    kept_call_ids = {
        call["id"]
        for m in kept
        if isinstance(m, AIMessage)
        for call in (m.tool_calls or [])
    }
    check(
        "context_fold_keeps_tool_pairs",
        all(isinstance(m, ToolMessage) and m.tool_call_id in kept_call_ids for m in kept if isinstance(m, ToolMessage)),
        f"cut={cut} kept={[type(m).__name__ for m in kept]}",
    )

    with _tempfile.TemporaryDirectory() as td:
        ctx_settings = settings.model_copy(
            update={
                "data_dir": Path(td) / "data",
                "context_soft_tokens": 10,
                "context_hard_tokens": 20,
                "context_keep_recent_messages": 2,
                "context_keep_recent_tokens": 50,
                "context_tool_message_max_chars": 8,
            }
        )
        ctx_settings.ensure_dirs()
        budget = ContextBudgetMiddleware(ctx_settings)
        update = budget.before_model({"messages": _msg_chain()})
        removals = (update or {}).get("messages") or []
        check(
            "context_soft_triggers_one_by_one_remove",
            bool(removals)
            and all(isinstance(r, RemoveMessage) and r.id != "__remove_all__" for r in removals),
            f"removals={len(removals)}",
        )
        check(
            "context_fold_writes_boundary_record",
            (ctx_settings.memory_dir / "context-boundaries.jsonl").exists(),
            str(ctx_settings.memory_dir),
        )

        # 替换式折叠（仅 router）：默认关闭 → 折叠只删不注；开启 → 追加记忆快照。
        check(
            "context_fold_rehydrate_default_off",
            not any(
                isinstance(m, HumanMessage) and m.additional_kwargs.get("context_rehydrate")
                for m in removals
            ),
            f"removals={len(removals)}",
        )
        (ctx_settings.memory_dir / "summary.md").write_text(
            "用户偏好暗色主题；主项目在 D:\\work。", encoding="utf-8"
        )
        rehydrating = ContextBudgetMiddleware(ctx_settings, rehydrate_memory=True)
        update2 = rehydrating.before_model({"messages": _msg_chain()}) or {}
        msgs2 = update2.get("messages") or []
        rehydrate_msgs = [
            m
            for m in msgs2
            if isinstance(m, HumanMessage) and m.additional_kwargs.get("context_rehydrate")
        ]
        check(
            "context_fold_rehydrate_appends_snapshot_once",
            len(rehydrate_msgs) == 1
            and "滚动摘要" in str(rehydrate_msgs[0].content)
            and "用户偏好暗色主题" in str(rehydrate_msgs[0].content),
            f"n={len(rehydrate_msgs)} content={str(getattr(rehydrate_msgs[0], 'content', ''))[:60]}",
        )

        class _Req:
            tool_call = {"name": "new_context", "args": {"keep_recent": 2}, "id": "call_nc"}
            state = {"messages": _msg_chain()}

        forced = budget.wrap_tool_call(_Req(), lambda req: None)
        check(
            "new_context_intercepted_not_executed",
            isinstance(forced, ToolMessage) and "已请求折叠" in str(forced.content),
            str(getattr(forced, "content", ""))[:80],
        )
        forced_removals = (budget.before_model(_Req.state) or {}).get("messages") or []
        check(
            "new_context_uses_per_message_removal",
            bool(forced_removals)
            and len({r.id for r in forced_removals}) == len(forced_removals)
            and all(r.id != "__remove_all__" for r in forced_removals),
            f"removals={len(forced_removals)}",
        )

    # ------------------------------------------------ WorkerInbox / StopInjection / SendMessage（§5.5/§5.6）
    from agents.middleware import StopInjectionMiddleware, WorkerInboxMiddleware  # noqa: E402

    class _Control:
        def __init__(self) -> None:
            self.items = ["顺便看一下磁盘剩余空间"]
            self._stop = False
            self._injected = False

        def drain_instructions(self):
            items, self.items = self.items, []
            return items

        def stop_requested(self):
            return self._stop

        def injected(self):
            return self._injected

        def mark_injected(self):
            self._injected = True

    control = _Control()
    inbox_update = WorkerInboxMiddleware(control).before_model({"messages": []})
    inbox_messages = (inbox_update or {}).get("messages") or []
    check(
        "worker_inbox_injects_instruction",
        len(inbox_messages) == 1
        and isinstance(inbox_messages[0], HumanMessage)
        and "磁盘剩余空间" in str(inbox_messages[0].content),
        str(inbox_messages),
    )

    control._stop = True
    stopper = StopInjectionMiddleware(control)
    stop_update = stopper.before_model({"messages": []})
    wrap_up = ((stop_update or {}).get("messages") or [None])[0]
    check(
        "stop_injection_wrap_up_injected",
        isinstance(wrap_up, HumanMessage) and "收尾" in str(wrap_up.content),
        str(wrap_up)[:80],
    )
    check("stop_injection_marks_level2", control.injected())
    check("stop_injection_only_once", stopper.before_model({"messages": []}) is None)

    class _ToolReq:
        tool_call = {"name": "Shell", "args": {"command": "dir"}, "id": "call_stop"}

    blocked = stopper.wrap_tool_call(_ToolReq(), lambda req: "EXECUTED")
    check(
        "stop_blocks_new_tool_calls",
        isinstance(blocked, ToolMessage) and "停止" in str(blocked.content),
        str(getattr(blocked, "content", ""))[:80],
    )

    import tools.worker_messages as worker_messages_mod  # noqa: E402

    class _MsgBus:
        def __init__(self, client=None) -> None:
            self.events: list = []

        def xadd(self, event, stream=None, maxlen=None):
            self.events.append(event)

    _orig_msg_bus = worker_messages_mod.Bus
    worker_messages_mod.Bus = _MsgBus  # type: ignore[assignment]
    try:
        msg_task = TaskSpec(
            task_id="01MSGTASK", prompt="p", title="t", trace_id="tr",
            cwd=".", workspace_root=".", chat_id="oc_MSG",
        )
        send = worker_messages_mod.build_send_message_tool(redis_client=None, task=msg_task)
        ask_result = send.invoke({"kind": "ask", "text": "要删掉旧备份吗？"})
        notify_result = send.invoke({"kind": "notify", "text": "已完成 3/10"})
    finally:
        worker_messages_mod.Bus = _orig_msg_bus  # type: ignore[assignment]

    check("send_message_ask_writes_worker_message", ask_result.get("kind") == "ask", str(ask_result))
    check("send_message_notify_writes_worker_notice", notify_result.get("kind") == "notify", str(notify_result))

    _orig_msg_bus2 = worker_messages_mod.Bus
    capture = _MsgBus()
    worker_messages_mod.Bus = lambda client: capture  # type: ignore[assignment]
    try:
        send2 = worker_messages_mod.build_send_message_tool(redis_client=None, task=msg_task)
        send2.invoke({"kind": "ask", "text": "x"})
        send2.invoke({"kind": "notify", "text": "y"})
    finally:
        worker_messages_mod.Bus = _orig_msg_bus2  # type: ignore[assignment]
    check(
        "send_message_event_types_and_chat_id",
        [e.type for e in capture.events] == ["worker_message", "worker_notice"]
        and all(e.payload.get("chat_id") == "oc_MSG" for e in capture.events),
        str([(e.type, e.payload.get("chat_id")) for e in capture.events]),
    )

    # ------------------------------------------------ 心跳 / 巡检（§5.7.3）
    from types import SimpleNamespace  # noqa: E402

    from agents.middleware import WorkerHeartbeatMiddleware  # noqa: E402

    beats = {"n": 0}
    WorkerHeartbeatMiddleware(lambda: beats.__setitem__("n", beats["n"] + 1)).before_model({"messages": []})
    check("worker_heartbeat_per_round", beats["n"] == 1, str(beats))

    import os as _os  # noqa: E402

    watchdog = main_gateway.TaskWatchdog(command_client=None, store=None)
    gone = SimpleNamespace(pid=999_999, last_seen=None, updated_at="2020-01-01T00:00:00+08:00")
    check("watchdog_process_gone", watchdog._detect(gone) == "process_gone", watchdog._detect(gone))

    stale = SimpleNamespace(
        pid=_os.getpid(),
        last_seen="2020-01-01T00:00:00+08:00",
        updated_at="2020-01-01T00:00:00+08:00",
    )
    check("watchdog_stalled", watchdog._detect(stale) == "stalled", watchdog._detect(stale))

    from contracts import now_iso  # noqa: E402

    fresh = SimpleNamespace(pid=_os.getpid(), last_seen=now_iso(), updated_at=now_iso())
    check("watchdog_fresh_heartbeat_ok", watchdog._detect(fresh) == "", watchdog._detect(fresh))

    # ------------------------------------------------ router 工具集（§6.1/§7.6）
    import inspect as _inspect  # noqa: E402

    from agents.router.graph import build_router  # noqa: E402
    from tools.shell import _MAX_ROUTER_TIMEOUT_MS  # noqa: E402

    router_src = _inspect.getsource(build_router)
    check("router_has_fs_tools", "build_fs_tools(" in router_src)
    check(
        "router_shell_short_command_profile",
        "build_shell_tools(" in router_src and 'profile="router"' in router_src,
    )
    check("router_shell_cap_30s", _MAX_ROUTER_TIMEOUT_MS == 30_000, str(_MAX_ROUTER_TIMEOUT_MS))

    # ------------------------------------------------ 任务类型注册表（task_kind）
    from agents import task_kinds as kinds  # noqa: E402

    check("task_kind_general", kinds.resolve_task_kind("general").kind == "general")
    check(
        "task_kind_memory",
        kinds.resolve_task_kind("memory_consolidate").kind == "memory_consolidate",
    )
    check(
        "task_kind_available_list",
        kinds.available_task_kinds() == ["general", "memory_consolidate", "cua"],
        str(kinds.available_task_kinds()),
    )
    # `cua` 已实现（§8）：解析必须成功，且工具集不是 general 那套。
    cua_spec = kinds.resolve_task_kind("cua")
    check("task_kind_cua_implemented", cua_spec.kind == "cua", cua_spec.kind)
    check(
        "task_kind_cua_not_general_tools",
        cua_spec.build_tools is not kinds._general_tools,
        "cua 仍复用 _general_tools",
    )
    try:
        kinds.resolve_task_kind("no-such-kind")
        unknown_error = ""
    except kinds.UnknownTaskKind as exc:
        unknown_error = str(exc)
    check(
        "task_kind_unknown_explicit",
        "未知的任务类型" in unknown_error and "general" in unknown_error,
        unknown_error or "没有报错",
    )
    check(
        "task_kind_registry_fields",
        all(
            spec.system_prompt and spec.build_tools and spec.default_cwd
            for spec in kinds._REGISTRY.values()
        ),
    )
    check(
        "task_kind_memory_prompt_is_type_specific",
        kinds._REGISTRY["memory_consolidate"].system_prompt is not kinds._REGISTRY["general"].system_prompt,
        "memory_consolidate 仍在复用通用 worker_prompt",
    )

    # ------------------------------------------------ 记忆：工具集与离线读写
    import json as _json  # noqa: E402
    import tempfile as _tempfile  # noqa: E402

    from memory import ledger  # noqa: E402
    from memory.jsonl import JsonlWriter  # noqa: E402
    from tools.memory import build_memory_router_tools, build_memory_worker_tools  # noqa: E402

    mem_names = [t.name for t in build_memory_worker_tools(settings)]
    check(
        "memory_worker_toolset",
        mem_names == ["ReadTurns", "ReadMemory", "WriteMemory"],
        str(mem_names),
    )
    router_mem_names = [
        t.name for t in build_memory_router_tools(settings=settings, session_service=None)
    ]
    check(
        "memory_router_toolset",
        router_mem_names == ["ReadRecords", "ReadMemory", "ConsolidateMemory"],
        str(router_mem_names),
    )

    with _tempfile.TemporaryDirectory(prefix="mem_check_") as td:
        memory_dir = Path(td) / "memory"
        memory_dir.mkdir(parents=True, exist_ok=True)
        writer = JsonlWriter(memory_dir, "turns")
        writer.append({"digest": "用户：我喜欢喝拿铁", "reply": "记住了", "tool_calls": [], "ok": True})
        writer.append({"digest": "用户：周报放在 D:/work", "reply": "收到", "tool_calls": [], "ok": True})

        fake_settings = SimpleNamespace(
            memory_dir=memory_dir,
            tool_outputs_dir=Path(td) / "out",
            tool_output_max_lines=2000,
            tool_output_max_bytes=64 * 1024,
        )
        read_turns, read_memory, write_memory = build_memory_worker_tools(fake_settings)

        read_all = read_turns.invoke({"since_seq": 0, "until_seq": 0, "limit": 10})
        check("memory_read_turns", read_all.get("count") == 2, str(read_all)[:160])
        wrote = write_memory.invoke(
            {
                "summary": "用户在整理周报，偏好拿铁。",
                "facts": [{"key": "coffee", "value": "拿铁", "confidence": "high", "source_turn": 1}],
                "decisions": [{"decision": "周报统一放 D:/work", "reason": "便于查找"}],
                "covers_seq": [1, 2],
            }
        )
        check(
            "memory_write_memory",
            wrote.get("summary_version") == 1
            and wrote.get("covers_seq") == [1, 2]
            and wrote.get("facts_written") == 1,
            str(wrote),
        )
        snapshot = read_memory.invoke({})
        check(
            "memory_readback",
            "拿铁" in snapshot.get("summary", "")
            and snapshot.get("facts") and snapshot["facts"][0].get("key") == "coffee"
            and snapshot.get("decisions"),
            str(snapshot)[:200],
        )
        report = _json.loads((memory_dir / "last-consolidation.json").read_text(encoding="utf-8"))
        check(
            "memory_consolidation_report",
            report.get("covers_seq") == [1, 2] and report.get("summary_version") == 1,
            str(report),
        )
        check("memory_state_cursor", ledger.last_consolidated_seq(memory_dir) == 2)
        check("memory_after_consolidation_no_pending", ledger.count_turns_after(memory_dir, 2) == 0)
        # router 回读：关键词命中 + 默认跳过元记录
        records_tool = build_memory_router_tools(
            settings=fake_settings, session_service=None
        )[0]
        hit = records_tool.invoke({"query": "拿铁"})
        check("memory_read_records_keyword", hit.get("count") == 1, str(hit)[:160])

    # ------------------------------------------------ 区域模型：可玩耍区 / 只读区 / 禁区
    from tools.fs import build_fs_tools  # noqa: E402
    from tools.rules import (  # noqa: E402
        PathForbidden,
        assert_not_forbidden,
        check_command_rules,
        is_forbidden,
    )

    check("rules_forbidden_system_dir", is_forbidden("C:\\Windows\\System32\\drivers\\etc\\hosts"))
    check("rules_forbidden_root_exact_only", is_forbidden("C:\\") and not is_forbidden("C:\\Users"))
    check("rules_temp_not_forbidden", not is_forbidden(str(Path(_tempfile.gettempdir()) / "x.txt")))
    try:
        assert_not_forbidden("C:\\Windows\\win.ini")
        check("rules_assert_forbidden", False, "未抛 PathForbidden")
    except PathForbidden as exc:
        check("rules_assert_forbidden", exc.error_class == "path_forbidden", str(exc))

    with _tempfile.TemporaryDirectory(prefix="region_check_") as td:
        base = Path(td)
        home = base / "home"
        (home / "Desktop").mkdir(parents=True)
        play = base / "play"
        play.mkdir()
        read_dir = base / "readonly"
        read_dir.mkdir()

        desktop = home / "Desktop"
        outside_file = base / "note.txt"
        inside_file = play / "note.txt"
        recursive_cmd = f'Remove-Item -Recurse -Force "{desktop}"'
        wildcard_cmd = f'del "{desktop}\\*.tmp"'
        outside_cmd = f'Set-Content -Path "{outside_file}" -Value x'
        inside_cmd = f'Set-Content -Path "{inside_file}" -Value x'

        hit = check_command_rules(recursive_cmd, workspace_root=play, home=home, write_roots=[play])
        check("rules_r2_personal_recursive_deny", hit is not None and hit.action == "deny", str(hit))
        hit = check_command_rules(wildcard_cmd, workspace_root=play, home=home, write_roots=[play])
        check("rules_r2_personal_wildcard_deny", hit is not None and hit.action == "deny", str(hit))
        hit = check_command_rules(outside_cmd, workspace_root=play, home=home, write_roots=[play])
        check("rules_r8_outside_play_confirm", hit is not None and hit.action == "need_confirm", str(hit))
        hit = check_command_rules(inside_cmd, workspace_root=play, home=home, write_roots=[play])
        check("rules_r8_inside_play_ok", hit is None, str(hit))

        fs = build_fs_tools(
            workspace_root=play,
            tool_outputs_dir=base / "out",
            read_roots=[read_dir, home / "Desktop"],
            write_roots=[play],
        )
        read_tool, write_tool, _edit, delete_tool, glob_tool, _grep = fs
        (read_dir / "doc.txt").write_text("只读区正文\n", encoding="utf-8", newline="\n")
        check("region_read_only_readable", read_tool.invoke({"path": str(read_dir / "doc.txt")}) == "只读区正文\n")
        res = write_tool.invoke({"path": str(read_dir / "x.txt"), "content": "x"})
        check("region_read_only_write_rejected", res.get("error_class") == "path_read_only", str(res))
        check("region_read_only_not_written", not (read_dir / "x.txt").exists())
        res = write_tool.invoke({"path": str(base / "y.txt"), "content": "y"})
        check("region_outside_all_rejected", res.get("error_class") == "path_outside_workspace", str(res))
        check(
            "region_glob_sees_read_only",
            any("doc.txt" in str(item) for item in glob_tool.invoke({"pattern": "*.txt", "path": str(read_dir)})),
        )
        many = []
        for i in range(11):
            target = play / f"del{i}.txt"
            target.write_text("x", encoding="utf-8")
            many.append(str(target))
        res = delete_tool.invoke({"paths": many})
        check(
            "region_delete_batch_cap",
            res.get("error_class") == "batch_too_large" and (play / "del0.txt").exists(),
            str(res)[:160],
        )

    # ------------------------------------------------ ReplyUser：文件/链接回传
    from agents.context import current_chat_id, current_trace_id  # noqa: E402
    from tools.reply import build_reply_user_tool  # noqa: E402

    class _FakeRedis:
        def __init__(self) -> None:
            self.store: dict[str, str] = {}

        def set(self, key, value, nx=False, xx=False, ex=None):
            if nx and key in self.store:
                return False
            if xx and key not in self.store:
                return False
            self.store[key] = value
            return True

        def get(self, key):
            return self.store.get(key)

        def delete(self, key):
            self.store.pop(key, None)

    class _FakeBus:
        def __init__(self) -> None:
            self.events: list = []

        def xadd(self, event, stream=None, maxlen=None):
            self.events.append(event)
            return "1-1"

    fake_bus = _FakeBus()
    reply_tool = build_reply_user_tool(bus=fake_bus, redis_client=_FakeRedis())
    current_chat_id.set("oc_test")
    current_trace_id.set("trace_test")
    items = ["D:/x/report.pdf", "https://example.com/a.html"]
    out = reply_tool.invoke({"text": "结果如下", "products": items})
    check("reply_products_queued", out.get("ok") is True and out.get("products") == items, str(out))
    check(
        "reply_products_in_event",
        bool(fake_bus.events) and fake_bus.events[0].payload.get("products") == items,
        str(fake_bus.events[:1]),
    )
    check("reply_empty_rejected", reply_tool.invoke({"text": "   "}).get("error_class") == "empty_text")
    only_files = reply_tool.invoke({"text": "", "products": ["D:/x/a.png"]})
    check("reply_files_only_allowed", only_files.get("ok") is True, str(only_files))

    # ------------------------------------------------ 定时任务：Schedule 工具
    from datetime import datetime, timedelta, timezone  # noqa: E402

    from store.sqlite import Store  # noqa: E402
    from tools.schedule import build_schedule_tools, next_run_iso  # noqa: E402

    with _tempfile.TemporaryDirectory(prefix="sched_check_") as td:
        store = Store(Path(td) / "s.db")
        store.init_schema()
        tools = build_schedule_tools(store=store)
        check("schedule_toolset", [t.name for t in tools] == ["Schedule"], str([t.name for t in tools]))
        schedule = tools[0]

        created = schedule.invoke({"action": "create", "cron": "0 9 * * *", "prompt": "每天提醒看邮件"})
        check(
            "schedule_create_cron",
            created.get("ok") is True and created.get("status") == "active" and created.get("next_run_utc"),
            str(created),
        )
        check(
            "schedule_create_missing_prompt",
            schedule.invoke({"action": "create", "cron": "* * * * *"}).get("error_class") == "missing_prompt",
        )
        check(
            "schedule_create_missing_trigger",
            schedule.invoke({"action": "create", "prompt": "x"}).get("error_class") == "missing_trigger",
        )
        check(
            "schedule_create_invalid_cron",
            schedule.invoke({"action": "create", "cron": "not a cron", "prompt": "x"}).get("error_class") == "invalid_cron",
        )
        check("schedule_invalid_action", schedule.invoke({"action": "nope"}).get("error_class") == "invalid_action")
        check("schedule_missing_id", schedule.invoke({"action": "pause"}).get("error_class") == "missing_schedule_id")
        check("schedule_not_found", schedule.invoke({"action": "cancel", "schedule_id": "nope"}).get("error_class") == "not_found")

        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds")
        one_shot = schedule.invoke({"action": "create", "at": past, "prompt": "一次性提醒"})
        due = store.list_due_schedules(
            datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        )
        check(
            "schedule_at_due_once",
            one_shot.get("ok") is True and any(r.id == one_shot["schedule_id"] for r in due),
            str(one_shot),
        )
        # 扫描器推进：一次性任务没有下一次 → done
        record = store.get_schedule(one_shot["schedule_id"])
        check("schedule_at_no_next", next_run_iso(record, after=datetime.now(timezone.utc)) is None)
        store.update_schedule_after_run(record.id, next_run_utc=None, ran_at_utc=datetime.now(timezone.utc).isoformat())
        check("schedule_at_done", store.get_schedule(record.id).status == "done")

        paused = schedule.invoke({"action": "pause", "schedule_id": created["schedule_id"]})
        check("schedule_pause", paused.get("status") == "paused")
        check("schedule_paused_not_due", not store.list_due_schedules("2999-01-01T00:00:00.000Z"))
        resumed = schedule.invoke({"action": "resume", "schedule_id": created["schedule_id"]})
        check(
            "schedule_resume_recomputes",
            resumed.get("status") == "active"
            and parse_iso(resumed.get("next_run_utc") or "") > datetime.now(timezone.utc),
            str(resumed),
        )
        listed = schedule.invoke({"action": "list"})
        check(
            "schedule_list",
            listed.get("ok") is True and listed.get("count") == 2 and len(listed.get("schedules") or []) == 2,
            str(listed)[:200],
        )
        cancelled = schedule.invoke({"action": "cancel", "schedule_id": created["schedule_id"]})
        check("schedule_cancel", cancelled.get("status") == "cancelled")

    # ------------------------------------------------ Skill：技能注册表 + 工具
    from skills.registry import SkillRegistry  # noqa: E402
    from tools.skill import build_skill_tools  # noqa: E402

    with _tempfile.TemporaryDirectory(prefix="skill_check_") as td:
        root = Path(td) / "skills"
        (root / "shared").mkdir(parents=True)
        (root / "router-only").mkdir(parents=True)
        (root / "shared" / "alpha.md").write_text(
            "---\nname: alpha\ndescription: 公共技能\nscope: both\n---\nALPHA BODY\n", encoding="utf-8"
        )
        (root / "router-only" / "beta.md").write_text(
            "---\nname: beta\ndescription: 只给 router\nscope: router\n---\nBETA BODY\n", encoding="utf-8"
        )
        (root / "broken.md").write_text("没有 frontmatter 的文件\n", encoding="utf-8")

        registry = SkillRegistry(root)
        router_skill = build_skill_tools(registry=registry, role="router")[0]
        worker_skill = build_skill_tools(registry=registry, role="worker")[0]
        check("skill_toolset", router_skill.name == "Skill" and worker_skill.name == "Skill")

        router_list = router_skill.invoke({})
        worker_list = worker_skill.invoke({})
        check(
            "skill_list_by_role",
            sorted(c["name"] for c in router_list) == ["alpha", "beta"]
            and [c["name"] for c in worker_list] == ["alpha"],
            str(router_list),
        )
        check(
            "skill_broken_skipped",
            sorted(c.name for c in registry.cards()) == ["alpha", "beta"],
            str([c.name for c in registry.cards()]),
        )
        loaded = router_skill.invoke({"name": "alpha"})
        check("skill_load_body", loaded.strip() == "ALPHA BODY", str(loaded))
        denied = worker_skill.invoke({"name": "beta"})
        check(
            "skill_scope_denied",
            denied.get("error_class") == "skill_unavailable" and "alpha" in (denied.get("message") or ""),
            str(denied),
        )
        check(
            "skill_unknown",
            router_skill.invoke({"name": "nope"}).get("error_class") == "skill_unavailable",
        )

    # ------------------------------------------------ read_session / TaskOutput：读 checkpoint
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402

    from config import Settings  # noqa: E402
    from tools.sessions import SessionService  # noqa: E402

    class _FakeSaver:
        def __init__(self, state=None, metadata=None):
            self._state = state
            self._meta = metadata or {}

        def get_tuple(self, config):
            if self._state is None:
                return None
            return type("_Tup", (), {"checkpoint": self._state, "metadata": self._meta, "config": config})()

    class _NullBus:
        def xadd(self, *args, **kwargs):
            return "1-1"

    with _tempfile.TemporaryDirectory(prefix="sess_check_") as td:
        settings = Settings(data_dir=Path(td))
        store = Store(Path(td) / "s.db")
        store.init_schema()
        store.upsert_session("worker:T1", title="t", status="done", cwd=td, first_message_at=None)

        def _service(saver):
            return SessionService(
                store=store, bus=_NullBus(), redis_client=None, settings=settings,
                repo_root=Path(td), checkpointer=saver,
            )

        convo = [
            HumanMessage(content="把报告整理一下"),
            AIMessage(content="好的，先看目录", tool_calls=[{"name": "Glob", "args": {}, "id": "c1"}]),
            ToolMessage(content="a.md\nb.md", tool_call_id="c1", name="Glob"),
            AIMessage(content="x" * 5000),
        ]
        state = {"id": "cp_1", "ts": "2026-11-09T00:00:00Z", "channel_values": {"messages": convo}}
        service = _service(_FakeSaver(state, {"step": 3}))

        out = service.read_session("T1")
        check(
            "session_read_roles",
            out.get("ok") is True
            and [m["role"] for m in out["messages"]] == ["user", "assistant", "assistant"]
            and out.get("source") == "checkpoint",
            str(out)[:300],
        )
        out_tools = service.read_session("worker:T1", include_tools=True)
        check(
            "session_read_include_tools",
            [m["role"] for m in out_tools["messages"]] == ["user", "assistant", "tool", "assistant"]
            and out_tools["total"] == 4,
            str([m["role"] for m in out_tools["messages"]]),
        )
        check(
            "session_read_truncates",
            out["messages"][-1].get("truncated") is True
            and out["messages"][-1]["content_chars"] == 5000
            and len(out["messages"][-1]["content"]) == settings.context_tool_message_max_chars,
            str(out["messages"][-1])[:200],
        )

        page1 = service.read_session("T1", limit=2)
        page2 = service.read_session("T1", limit=2, cursor=page1["next_cursor"])
        check(
            "session_read_paginates",
            page1["returned"] == 2 and page1["next_cursor"] == "2"
            and page2["returned"] == 1 and page2["next_cursor"] is None
            and page2["messages"][0]["role"] == "user",
            f"{page1.get('next_cursor')} / {page2.get('next_cursor')} / {page2.get('returned')}",
        )
        check(
            "session_read_invalid_cursor",
            service.read_session("T1", cursor="abc").get("error_class") == "invalid_cursor",
        )
        check("session_read_not_found", service.read_session("nope").get("error_class") == "not_found")

        # checkpoint 不可用 → 退回 result.json 的最终结论
        result_dir = settings.workers_dir / "T2"
        result_dir.mkdir(parents=True, exist_ok=True)
        (result_dir / "result.json").write_text(
            '{"status": "done", "text": "最终结论正文"}', encoding="utf-8"
        )
        store.upsert_session("worker:T2", title="t2", status="done", cwd=td, first_message_at=None)
        fallback = _service(None).read_session("T2")
        check(
            "session_read_result_fallback",
            fallback.get("source") == "result_json"
            and fallback["messages"][0]["content"] == "最终结论正文",
            str(fallback)[:300],
        )
        check(
            "session_read_no_data",
            _service(None).read_session("worker:T1").get("source") == "none",
        )

        store.set_first_message_at("worker:T1", "2026-11-09T00:00:00Z")
        out_ck = service.task_output("T1")
        ck = out_ck.get("last_checkpoint") or {}
        check(
            "task_output_checkpoint",
            out_ck.get("status") == "has_update"
            and ck.get("checkpoint_id") == "cp_1"
            and ck.get("step") == 3
            and ck.get("message_count") == 4
            and (ck.get("last_message") or {}).get("role") == "assistant",
            str(out_ck)[:300],
        )
        check(
            "task_output_checkpoint_truncated",
            len((ck.get("last_message") or {}).get("content") or "") == 300,
        )
        out_none = _service(None).task_output("T1")
        check(
            "task_output_no_checkpoint",
            out_none.get("last_checkpoint") is None and "checkpoint" in (out_none.get("note") or ""),
            str(out_none)[:200],
        )
        store.upsert_session("worker:T3", title="t3", status="running", cwd=td, first_message_at=None)
        check(
            "task_output_no_update",
            service.task_output("T3").get("status") == "no_update",
        )

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
