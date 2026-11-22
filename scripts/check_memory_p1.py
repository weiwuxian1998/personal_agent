"""记忆设计 P1 验收（docs/MEMORY_DESIGN.md）。

覆盖五块：
1. transcript 打标 + 按天流水（TranscriptMiddleware，§4/§8.2）：created_at 补标、
   transcript_journaled 幂等键、折叠注入块（自带 created_at）也入流水；
2. 目录布局（memory.layout）：turns/inbox 在 ledger 区、transcript/notes 各自就位；
3. 时间戳折叠纯函数（context_budget.nightly_fold_plan，§8.2）：cutoff 删除、
   memory_refresh 幂等删除、配对回吸、未打标保留；
4. 注入渲染（render_memory_injection）：handoff + index + threads 结构；
5. router 启动折叠（main_router._apply_nightly_fold）：昨夜 marker → 删旧 + 注入，
   二次启动靠 ledger 的 nightly_fold_day 幂等跳过，旧 marker 不误删。

运行：python scripts/check_memory_p1.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain_core.messages import (  # noqa: E402
    AIMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)

import main_router  # noqa: E402
from agents.context_budget import (  # noqa: E402
    nightly_fold_plan,
    parse_created_at,
    render_memory_injection,
)
from agents.transcript import TranscriptMiddleware, message_record  # noqa: E402
from config import settings as real_settings  # noqa: E402
from contracts import TZ_SHANGHAI, now_iso  # noqa: E402
from memory.layout import ledger_dir, notes_dir, transcript_dir  # noqa: E402

_PASS = 0
_FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" | {detail}" if detail and not ok else ""))
    _PASS += ok
    _FAIL += not ok


# ---------------------------------------------------------------- 时间基准
def _today_cutoff() -> datetime:
    return datetime.combine(datetime.now(TZ_SHANGHAI).date(), dtime.min, tzinfo=TZ_SHANGHAI)


# ---------------------------------------------------------------- 1. transcript
def check_transcript(root: Path) -> None:
    fake = SimpleNamespace(router_dir=root)
    mw = TranscriptMiddleware(fake)

    pre = SystemMessage(
        content="折叠注入块",
        id="s1",
        additional_kwargs={"created_at": "2026-11-22T05:00:00+08:00", "memory_refresh": True},
    )
    plain = HumanMessage(content="普通消息", id="h1")
    state = {"messages": [pre, plain]}

    upd = mw.before_model(state)
    check(
        "transcript_before_model_returns_both",
        upd is not None and len(upd["messages"]) == 2,
        str(upd),
    )
    check(
        "transcript_preStamped_keeps_created_at",
        pre.additional_kwargs["created_at"] == "2026-11-22T05:00:00+08:00",
        str(pre.additional_kwargs),
    )
    check(
        "transcript_journaled_flag_set",
        pre.additional_kwargs.get("transcript_journaled") is True
        and plain.additional_kwargs.get("transcript_journaled") is True,
        "",
    )
    check("transcript_plain_stamped", bool(plain.additional_kwargs.get("created_at")))
    # 二次进入：幂等，不再产出更新
    check("transcript_idempotent_second_pass", mw.before_model(state) is None)

    files = sorted(transcript_dir(fake).glob("transcript-*.jsonl"))
    check("transcript_file_created", len(files) == 1, str([f.name for f in files]))
    records = [json.loads(ln) for ln in files[0].read_text(encoding="utf-8").splitlines()]
    by_id = {r["msg_id"]: r for r in records}
    check("transcript_two_records", len(records) == 2, str(records))
    check(
        "transcript_preStamped_record_keeps_ts",
        by_id.get("s1", {}).get("created_at") == "2026-11-22T05:00:00+08:00",
        str(by_id.get("s1")),
    )
    check(
        "transcript_record_shape",
        set(by_id["h1"]).issuperset({"kind", "msg_id", "role", "created_at", "content"}),
        str(by_id["h1"]),
    )

    # after_model：末尾新 AIMessage 入流水；无 id 不回传但已打标
    ai = AIMessage(content="新回复")
    upd2 = mw.after_model({"messages": [pre, plain, ai]})
    check(
        "transcript_after_model_no_id_not_returned",
        upd2 is None or upd2.get("messages") == [],
        str(upd2),
    )
    check("transcript_after_model_stamped", ai.additional_kwargs.get("transcript_journaled") is True)

    # message_record：图片块降级为占位，不含 base64
    img = HumanMessage(
        content=[{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}],
        id="img1",
    )
    rec = message_record(img)
    check(
        "transcript_image_block_placeholder",
        isinstance(rec["content"], list)
        and rec["content"][0].get("type") == "image"
        and "base64" not in json.dumps(rec["content"]),
        str(rec["content"]),
    )


# ---------------------------------------------------------------- 2. layout
def check_layout(root: Path) -> None:
    fake = SimpleNamespace(router_dir=root, memory_dir=root / "memory")
    check("layout_ledger_under_router", ledger_dir(fake) == root / "ledger")
    check("layout_transcript_under_router", transcript_dir(fake) == root / "transcript")
    check("layout_notes_under_router", notes_dir(fake) == root / "notes")


# ---------------------------------------------------------------- 3/4. fold 纯函数
def check_fold_pure() -> None:
    cutoff = _today_cutoff()
    yest = (cutoff - timedelta(hours=6)).isoformat()
    today = datetime.now(TZ_SHANGHAI).isoformat()

    msgs = [
        SystemMessage(content="旧注入", id="m0", additional_kwargs={"created_at": yest, "memory_refresh": True}),
        HumanMessage(content="凌晨提问", id="m1", additional_kwargs={"created_at": yest}),
        AIMessage(content="", id="m2", tool_calls=[{"id": "t1", "name": "x", "args": {}}], additional_kwargs={"created_at": yest}),
        ToolMessage(content="结果", id="m3", tool_call_id="t1", additional_kwargs={"created_at": today}),
        AIMessage(content="凌晨回复", id="m4", additional_kwargs={"created_at": today}),
        HumanMessage(content="无时间戳"),
    ]
    plan = nightly_fold_plan(msgs, cutoff=cutoff)
    check("fold_deletes_injection_and_old_human", plan.delete_ids == ["m0", "m1"], str(plan.delete_ids))
    check("fold_pair_snap_keeps_ai", plan.snapped_back == 1 and plan.kept == 4, str(plan))

    plan2 = nightly_fold_plan(
        [
            HumanMessage(content="昨天", id="a1", additional_kwargs={"created_at": yest}),
            AIMessage(content="昨天回复", id="a2", additional_kwargs={"created_at": yest}),
            HumanMessage(content="今天", id="a3", additional_kwargs={"created_at": today}),
        ],
        cutoff=cutoff,
    )
    check("fold_plain_delete", plan2.delete_ids == ["a1", "a2"] and plan2.snapped_back == 0, str(plan2))

    plan3 = nightly_fold_plan(
        [
            SystemMessage(content="inj1", id="b1", additional_kwargs={"memory_refresh": True, "created_at": today}),
            SystemMessage(content="inj2", id="b2", additional_kwargs={"memory_refresh": True, "created_at": today}),
            HumanMessage(content="今天", id="b3", additional_kwargs={"created_at": today}),
        ],
        cutoff=cutoff,
    )
    check("fold_removes_all_injections_idempotent", plan3.delete_ids == ["b1", "b2"], str(plan3))

    naive = parse_created_at(HumanMessage(content="x", additional_kwargs={"created_at": "2026-11-21T08:00:00"}))
    check("fold_parse_naive_gets_tz", naive is not None and naive.tzinfo is not None and naive < cutoff)
    check("fold_parse_bad_returns_none", parse_created_at(HumanMessage(content="x", additional_kwargs={"created_at": "junk"})) is None)
    check("fold_parse_missing_returns_none", parse_created_at(HumanMessage(content="x")) is None)

    text = render_memory_injection(
        handoff="今天记得提醒开会。",
        index_text="# index\n- 页A",
        threads=[("threads-2026-11-20.md", "# 旧"), ("threads-2026-11-21.md", "# 新")],
        day="2026-11-21",
    )
    check(
        "inject_structure",
        "<memory_refresh>" in text
        and "今天记得提醒开会" in text
        and "index" in text
        and "threads-2026-11-21.md" in text
        and text.index("旧") < text.index("新"),
        text[:120],
    )
    empty = render_memory_injection(handoff="", index_text="  ", threads=[], day="2026-11-21")
    check("inject_empty_only_frame", "## 交接" not in empty and "<memory_refresh>" in empty)


# ---------------------------------------------------------------- 5. _apply_nightly_fold
class _FakeGraph:
    def __init__(self, messages: list) -> None:
        self.messages = messages
        self.update_calls: list[list] = []

    def get_state(self, config):  # noqa: ANN001, ANN201
        return SimpleNamespace(values={"messages": list(self.messages)})

    def update_state(self, config, values):  # noqa: ANN001, ANN201
        ups = values.get("messages") or []
        self.update_calls.append(ups)
        remove_ids = {u.id for u in ups if isinstance(u, RemoveMessage)}
        self.messages = [m for m in self.messages if getattr(m, "id", None) not in remove_ids]
        for u in ups:
            if not isinstance(u, RemoveMessage):
                self.messages.append(u)


def check_apply_fold(root: Path) -> None:
    # memory_dir / router_dir 是从 data_dir 派生的 property（config.py 冻结），
    # 所以这里 model_copy 只改 data_dir：router 区 = root/router，记忆区 = root/router/memory。
    root = root / "data"
    mem = root / "router" / "memory"
    (mem / "threads").mkdir(parents=True, exist_ok=True)
    (mem / "index.md").write_text("# 记忆索引\n- 页A", encoding="utf-8")
    (mem / "threads" / "threads-2026-11-21.md").write_text("# threads", encoding="utf-8")

    fake_settings = real_settings.model_copy(
        update={"data_dir": root, "router_thread_id": "router:main"}
    )
    orig = main_router.settings
    main_router.settings = fake_settings
    try:
        yesterday = (datetime.now(TZ_SHANGHAI).date() - timedelta(days=1)).isoformat()
        marker = {
            "v": 2,
            "status": "done",
            "consolidated_day": yesterday,
            "finished_at": now_iso(),
            "threads_file": "threads/threads-2026-11-22.md",
            "pages_touched": ["index.md"],
            "handoff": "早上好，昨天讨论了记忆设计。",
        }
        (mem / "last-consolidation.json").write_text(
            json.dumps(marker, ensure_ascii=False), encoding="utf-8"
        )

        cutoff = _today_cutoff()
        yest = (cutoff - timedelta(hours=6)).isoformat()
        today = datetime.now(TZ_SHANGHAI).isoformat()
        graph = _FakeGraph(
            [
                HumanMessage(content="昨天", id="x1", additional_kwargs={"created_at": yest}),
                AIMessage(content="昨天回复", id="x2", additional_kwargs={"created_at": yest}),
                HumanMessage(content="今天", id="x3", additional_kwargs={"created_at": today}),
            ]
        )
        main_router._apply_nightly_fold(graph, saver=object())
        check("apply_fold_called_update", len(graph.update_calls) == 1, str(len(graph.update_calls)))
        injected = [m for m in graph.messages if isinstance(m, SystemMessage) and m.additional_kwargs.get("memory_refresh")]
        check("apply_fold_injected_one", len(injected) == 1, str(len(injected)))
        check("apply_fold_injection_has_handoff", "早上好" in injected[0].content)
        ids_left = {getattr(m, "id", None) for m in graph.messages}
        check("apply_fold_removed_old", "x1" not in ids_left and "x2" not in ids_left, str(ids_left))
        check("apply_fold_kept_today", "x3" in ids_left, str(ids_left))
        check(
            "apply_fold_recorded_nightly_day",
            json.loads((ledger_dir(fake_settings) / "state.json").read_text(encoding="utf-8")).get("nightly_fold_day")
            == yesterday,
            "",
        )

        # 二次启动：幂等跳过（不再 update）
        graph2 = _FakeGraph([HumanMessage(content="今天", id="y1", additional_kwargs={"created_at": today})])
        main_router._apply_nightly_fold(graph2, saver=object())
        check("apply_fold_idempotent_second_start", graph2.update_calls == [], str(graph2.update_calls))

        # 旧 marker（前天）：不误删，直接跳过
        old_day = (datetime.now(TZ_SHANGHAI).date() - timedelta(days=2)).isoformat()
        (mem / "last-consolidation.json").write_text(
            json.dumps({**marker, "consolidated_day": old_day}, ensure_ascii=False), encoding="utf-8"
        )
        # 清掉幂等游标，逼它重新评估 marker 日期
        (ledger_dir(fake_settings) / "state.json").unlink()
        graph3 = _FakeGraph([HumanMessage(content="昨天", id="z1", additional_kwargs={"created_at": yest})])
        main_router._apply_nightly_fold(graph3, saver=object())
        check("apply_fold_skips_stale_marker", graph3.update_calls == [], str(graph3.update_calls))

        # 无 checkpoint（saver=None）：直接返回
        graph4 = _FakeGraph([])
        main_router._apply_nightly_fold(graph4, saver=None)
        check("apply_fold_no_saver_noop", graph4.update_calls == [], str(graph4.update_calls))
    finally:
        main_router.settings = orig


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="mem_p1_") as td:
        root = Path(td)
        print("== transcript 打标 + 流水 ==")
        check_transcript(root / "t1")
        print("== 目录布局 ==")
        check_layout(root / "t2")
        print("== 折叠纯函数 ==")
        check_fold_pure()
        print("== router 启动折叠 ==")
        check_apply_fold(root / "t3")
    print()
    print(f"{_PASS} passed, {_FAIL} failed")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
