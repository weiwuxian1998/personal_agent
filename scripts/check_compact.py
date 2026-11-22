"""Worker 三级压缩（agents/compact.py）验收：

- token 估算的 usage 精确基线；
- L1：长 ToolMessage 截断、落盘指针保留、幂等、达标即停；
- lazy L1 不够时走 L2（假模型）：批次摘要替换、配对不拆、标记幂等；
- L3 兜底：无可摘范围时全量压缩；
- 两段式输出剥离（<analysis> 草稿丢弃、<summary> 入上下文）。

运行：python scripts/check_compact.py
"""

from __future__ import annotations

import re
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402

from agents.compact import (  # noqa: E402
    WorkerCompactMiddleware,
    estimate_context_tokens,
    extract_summary,
    find_protect_boundary,
)
from config import Settings  # noqa: E402

_PASS = 0
_FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" | {detail}" if detail and not ok else ""))
    _PASS += ok
    _FAIL += not ok


# ---------------------------------------------------------------- 构造工具
def _filler(chars: int) -> str:
    return ("line " + "x" * 20 + "\n") * (chars // 26)


def _round(tool_content: str, *, human: str = "继续任务", usage: int | None = None) -> list:
    call_id = uuid.uuid4().hex
    ai = AIMessage(
        id=uuid.uuid4().hex,
        content="",
        tool_calls=[{"name": "Shell", "args": {"command": "dir"}, "id": call_id, "type": "tool_call"}],
    )
    if usage:
        ai.usage_metadata = {"input_tokens": usage, "output_tokens": 10, "total_tokens": usage + 10}
    tool = ToolMessage(
        id=uuid.uuid4().hex, content=tool_content, tool_call_id=call_id, name="Shell"
    )
    return [HumanMessage(id=uuid.uuid4().hex, content=human), ai, tool]


def _make_middleware(tmp: Path, **overrides) -> WorkerCompactMiddleware:
    kwargs = dict(
        data_dir=tmp,
        context_window_tokens=10_000,
        compact_trigger_ratio=0.5,
        compact_target_ratio=0.3,
        compact_l2_batch_ratio=0.3,
        compact_protect_rounds=1,
        compact_preview_chars=200,
        compact_min_compress_chars=100,
        compact_summary_chars=500,
        compact_l3_summary_chars=300,
    )
    kwargs.update(overrides)
    settings = Settings(**kwargs)
    return WorkerCompactMiddleware(settings, system_prompt="sys-prompt", tools=[])


class _FakeModel:
    """假摘要模型：记录调用与 bind_tools 参数，返回两段式输出。"""

    def __init__(self) -> None:
        self.calls: list[list] = []
        self.bound_tools: list | None = None

    def bind_tools(self, tools):
        self.bound_tools = list(tools)
        return self

    def invoke(self, messages):
        self.calls.append(list(messages))
        return AIMessage(content="<analysis>草稿，不应进入上下文</analysis>\n<summary>这是结构化摘要</summary>")


class _BoomModel:
    def bind_tools(self, tools):
        return self

    def invoke(self, messages):  # pragma: no cover - 断言不被调用
        raise AssertionError("摘要模型不应被调用")


def main() -> int:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)

        # ------------------------------------------------------ token 估算
        messages: list = []
        messages += _round(_filler(2000))
        messages += _round(_filler(2000), usage=1000)
        messages += _round(_filler(4000))
        est = estimate_context_tokens(messages)
        check("token 估算走 usage 基线（1000 + 尾部估算）", 1000 < est < 1000 + 4000, str(est))
        check(
            "保护区边界：protect_rounds=1 边界在最后一条 AI 消息；轮次不足时为 0",
            find_protect_boundary(messages, 1) == len(messages) - 2
            and find_protect_boundary(messages, 99) == 0,
        )

        # ------------------------------------------------------ L1（单独够用，不调用摘要模型）
        mw = _make_middleware(tmp)
        mw._model = _BoomModel()
        stub = (
            "[shell] exit=0  total_lines=500\n"
            "--- head ---\n" + _filler(6000)
            + "--- tail ---\nfoo\n[artifact] C:\\out\\sh_1.txt\n用 Read 分页读取。"
        )
        plain = _filler(6000)
        l1_msgs: list = []
        for content in (stub, plain, stub, plain):
            l1_msgs += _round(content)
        # 第 5 轮受保护；usage 基线 6000 + 尾部 → 总量约 7.5k，目标 3k，需释放 ~4.5k
        l1_msgs += _round(stub, usage=6000)
        update = mw.before_model({"messages": l1_msgs})
        check("L1 触发并返回更新", update is not None)
        if update:
            new_msgs = update["messages"]
            compacted = [
                m for m in new_msgs if isinstance(m, ToolMessage) and "compacted:L1" in m.content
            ]
            check("4 条保护区外的长工具输出全部截断", len(compacted) == 4, str(len(compacted)))
            body = compacted[0].content
            check("artifact 指针行保留", "[artifact] C:\\out\\sh_1.txt" in body, body[:200])
            check("预览截到 preview_chars 量级", len(body) < 600, str(len(body)))
            saved = [m for m in compacted if "完整内容已存盘" in m.content]
            check("未落盘输出已存盘并留指针", len(saved) == 2, str(len(saved)))
            paths = re.findall(r"已存盘 (\S+?\.txt)", "".join(m.content for m in saved))
            check("存盘文件真实存在", len(paths) == 2 and all(Path(p).exists() for p in paths), str(paths))
            check("原 ToolMessage id 保留（原位更新）", all(m.id for m in new_msgs))
            second = mw.before_model({"messages": new_msgs})
            check("L1 幂等：压缩后低于触发水位，不再触发", second is None)

        # ------------------------------------------------------ L2（L1 不够，假模型摘要）
        mw2 = _make_middleware(tmp, compact_min_compress_chars=20_000)  # L1 无候选 → 必然不够
        fake = _FakeModel()
        mw2._model = fake
        mw2._tools = ["worker_tool_x"]  # 模拟真实 tools，验证 invoke 处显式绑定
        fake.bind_tools(mw2._tools)  # noqa: 预先记录，实际绑定在 _summarize 内发生
        fake.bound_tools = None
        l2_msgs: list = []
        for _ in range(4):
            l2_msgs += _round(_filler(6000), human=_filler(8000))  # 无 usage → 全量估算超触发
        l2_msgs += _round(_filler(200))  # 最后一轮受保护
        update2 = mw2.before_model({"messages": l2_msgs})
        check("L1 不够 → L2 触发并返回更新", update2 is not None)
        if update2:
            new2 = update2["messages"]
            summary = [
                m for m in new2 if isinstance(m, HumanMessage) and "compacted:L2" in m.content
            ]
            check("批次被替换为一条 L2 摘要", len(summary) == 1, str(len(summary)))
            if summary:
                check("<analysis> 草稿已剥离", "草稿" not in summary[0].content)
                check("<summary> 正文保留", "这是结构化摘要" in summary[0].content)
            check("保护区最后一条 AI 消息未动", new2[-2].type == "ai" and new2[-1].content == l2_msgs[-1].content)
            check("摘要请求带系统提示词前缀（cache 命中）", fake.calls and fake.calls[0][0].content == "sys-prompt")
            check("指令 HumanMessage 追加在批次末尾", isinstance(fake.calls[0][-1], HumanMessage))
            check("摘要请求复用完整对话批次（前缀一致）", len(fake.calls[0]) > 3)
            check("摘要请求显式带上 tools（cache 前缀一致）", fake.bound_tools == ["worker_tool_x"])

        # ------------------------------------------------------ L3 兜底（可摘范围不足一个窗口）
        mw3 = _make_middleware(
            tmp, compact_l2_batch_ratio=0.99, compact_min_compress_chars=20_000  # L1 无候选
        )
        fake3 = _FakeModel()
        mw3._model = fake3
        l3_msgs: list = [HumanMessage(content="原始任务")]
        for _ in range(4):
            l3_msgs += _round(_filler(6000))
        update3 = mw3.before_model({"messages": l3_msgs})
        check("L2 跳过（范围不足窗口）→ L3 兜底触发", update3 is not None)
        if update3:
            new3 = [m for m in update3["messages"] if m.type != "remove"]
            l3s = [
                m for m in new3 if isinstance(m, HumanMessage) and "compacted:L3" in m.content
            ]
            check("全部历史压成一条 L3 摘要", len(l3s) == 1, str(len(l3s)))
            check("首条消息保留", new3[0].content == "原始任务")

        # ------------------------------------------------------ 两段式剥离
        check(
            "extract_summary：无 summary 块时剥离 analysis",
            extract_summary("<analysis>x</analysis>裸摘要") == "裸摘要",
        )
        check("extract_summary：无任何块时原文兜底", extract_summary("plain") == "plain")

    print(f"\n{_PASS} passed, {_FAIL} failed")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
