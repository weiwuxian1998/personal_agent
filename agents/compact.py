"""Worker 三级上下文压缩（对齐 Claude Code compaction 级联）。

worker 会跨任务复用（相似任务 / 二次修改），上下文经 checkpointer 持久化，
所以语义摘要必须做；删除式折叠（ContextBudgetMiddleware）只留作最后防线。

每次模型调用前检查上下文用量（以最后一次 ``usage_metadata.input_tokens`` 为
精确基线，叠加其后新增消息的估算；无 usage 时回退全量字符估算），按水位递进：

- L1（零 LLM 成本，≈ Claude Code T1/T2）：超触发水位时，把保护区外的长
  ToolMessage 截断为预览。已落盘的输出（shell/web 的 ``[artifact]``、fs 的
  【完整内容已落盘】）保留指针；未落盘的存到 ``tool_outputs/compact/`` 留指针。
- L2（批量 LLM 摘要，≈ T3/T4）：L1 不够时触发。单批窗口 = 窗口 ×
  ``compact_l2_batch_ratio``（比例化，随窗口缩放），且至少覆盖 current - target
  的超额量。摘要请求 = ``[SystemMessage(系统提示词)] + 历史批次 +
  [HumanMessage(指令)]``：指令追加在已有上下文**末尾**、复用 agent 同一模型与
  工具定义 → 请求前缀与主对话完全一致，完整命中 KV cache（Claude Code fork
  子代理的同款思路）。摘要按 Claude Code 结构化模板裁剪，两段式输出
  （``<analysis>`` 草稿 + ``<summary>`` 正文），注入时剥离草稿。渐进式：
  每次只摘一个批次，多轮逐步退化，不做断崖式全压。
- L3（全量兜底）：L2 无可摘范围或失败时，把保护区前全部历史压成一条摘要。
- 全部失败时不返回任何更新，ContextBudgetMiddleware 折叠作为最后防线。

压缩标记 ``<!-- compacted:L1 -->`` / ``:L2`` / ``:L3`` 幂等，已压消息不重复处理。
摘要直接走 model client、不经过 agent graph，天然没有递归压缩风险
（Claude Code 需要 recursion guard 是因为它走完整 agent 循环，这里不需要）。
"""

from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)

from agents.base import build_chat_model
from agents.context_budget import estimate_text_tokens, estimate_tokens
from config import Settings

logger = logging.getLogger("agents.compact")

# 摘要调用超时（秒）：非思考模式 + cache 命中的单次摘要 60s 稳稳够用，
# 防止摘要端点异常卡死 worker 的模型循环；超时走 except 降级到下一层兜底。
_SUMMARY_TIMEOUT_SECONDS = 60.0

_COMPACT_RE = re.compile(r"<!-- compacted:L\d+ -->")
_L1_MARKER = "<!-- compacted:L1 -->"
_L2_MARKER = "<!-- compacted:L2 -->"
_L3_MARKER = "<!-- compacted:L3 -->"

# 工具输出已落盘的两种 stub 指针（tools.shell.stub_text 的 `[artifact] <path>`
# 与 tools.fs._stub 的 【完整内容已落盘】<path>）。命中即认为全文可回查，
# L1 只截正文、保留指针行。
_ARTIFACT_RE = re.compile(
    r"^(?:\[artifact\] \S.+|【完整内容已落盘】\S.+)$",
    re.MULTILINE,
)

# L2 摘要指令：追加在已有上下文末尾（不单独构造请求前缀 → KV cache 全命中）。
_L2_SUMMARY_PROMPT = """<system-reminder>
[系统指令：上下文即将超限。当前唯一任务：对以上对话历史生成结构化摘要，而非继续执行原有任务。

上下文中带 <!-- compacted --> 标记的部分已完成压缩，无需重复处理，仅关注其后尚未压缩的新增内容。

先在 <analysis> 块中按时间顺序梳理对话、判断哪些必须保留（此块是草稿，不会进入后续上下文），
再在 <summary> 块中输出结构化摘要，包含：
1. 核心任务与用户意图（用户消息保留原文要点，不得转述失真）
2. 关键决策与约定
3. 重要文件信息：精确路径 + 用途 + 关键内容片段（仅列对后续推进有直接影响的；
   过程性、临时性、可用工具重新获取的不得列入）
4. 已遇到的问题与修复方式
5. 已完成步骤与重要中间结果
6. 未完成步骤与下一步计划

丢弃所有可重新推导的细节。文件路径必须精确保留。
摘要语言与用户提问语言一致。目标 {target_chars} 字符以内。]
</system-reminder>"""

# L3 摘要指令：全量压缩（含此前已生成的 L2 摘要，一并合并精简）。
_L3_SUMMARY_PROMPT = """<system-reminder>
[系统指令：对以上全部对话历史（含早期压缩摘要）做最终的高度压缩摘要。当前唯一任务是生成摘要。

先在 <analysis> 块中梳理（草稿，不进入后续上下文），再在 <summary> 块中输出：
1. 核心任务与用户意图（用户消息保留原文要点）
2. 关键决策与约定
3. 重要文件信息：精确路径 + 用途
4. 重要结果与未解决问题
5. 下一步计划

积极精简，丢弃所有可重新推导的细节。文件路径必须精确保留。
摘要语言与用户提问语言一致。目标 {target_chars} 字符以内。]
</system-reminder>"""


def _content_str(msg: Any) -> str:
    """把消息内容抽成纯文本（str / list[str] / list[dict] 均兼容）。"""
    content = getattr(msg, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(str(block.get("text") or ""))
        return "".join(parts)
    return str(content or "")


def estimate_context_tokens(messages: list[Any]) -> int:
    """估算当前上下文 token 数：usage 精确基线 + 其后新增消息估算。

    取最后一条 AIMessage 的 ``usage_metadata.input_tokens``（API 精确值，覆盖
    该次请求的全部输入），再叠加它自身及其后所有消息的字符估算——宁可高估，
    不要低估（高估只是提前压缩，低估会撞 413）。
    """
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if getattr(msg, "type", "") != "ai" and not isinstance(msg, AIMessage):
            continue
        usage = getattr(msg, "usage_metadata", None)
        if usage and usage.get("input_tokens"):
            return int(usage["input_tokens"]) + estimate_tokens(messages[i:])
    return estimate_tokens(messages)


def find_protect_boundary(messages: list[Any], protect_rounds: int) -> int:
    """保护区起始索引（该索引及其后的消息不压缩）。

    从后往前数 AI 轮次，返回第 protect_rounds 轮 AI 消息的位置；不足
    protect_rounds 轮时返回 0（整个历史都在保护区内，无可压缩）。
    边界由 AI 消息承担，其后的 tool_call / ToolMessage 配对天然完整。
    """
    ai_rounds = 0
    for i in range(len(messages) - 1, 0, -1):
        if getattr(messages[i], "type", "") == "ai":
            ai_rounds += 1
            if ai_rounds >= protect_rounds:
                return i
    return 0


def _truncate_lines(lines: list[str], max_chars: int) -> int:
    """返回从头保留的行数，总字符（含换行）不超过 max_chars。"""
    keep = 0
    total = 0
    for line in lines:
        cost = len(line) + 1
        if keep > 0 and total + cost > max_chars:
            break
        total += cost
        keep += 1
    return keep


def extract_summary(text: str) -> str:
    """两段式输出剥离：取 <summary> 正文；缺失时剥掉 <analysis> 草稿再兜底原文。"""
    match = re.search(r"<summary>(.*?)</summary>", text, re.DOTALL)
    if match and match.group(1).strip():
        return match.group(1).strip()
    stripped = re.sub(r"<analysis>.*?</analysis>", "", text, flags=re.DOTALL).strip()
    return stripped or text.strip()


class WorkerCompactMiddleware(AgentMiddleware):
    """L1 截断 → L2 批量摘要 → L3 全量摘要，全部失败时交给折叠兜底。

    用主模型做摘要（ds-flash 便宜且与 agent 完全同款请求形状 → cache 全命中）。
    worker 以同步 ``graph.stream`` 运行，摘要用同步 ``model.invoke``：阻塞一次
    摘要调用对后台任务可接受，且不用维护同步/异步两条路径。
    """

    def __init__(self, settings: Settings, *, system_prompt: str, tools: list[Any]) -> None:
        super().__init__()
        self._context_window = int(settings.context_window_tokens)
        self._trigger_ratio = float(settings.compact_trigger_ratio)
        self._target_ratio = float(settings.compact_target_ratio)
        self._l2_batch_ratio = float(settings.compact_l2_batch_ratio)
        self._protect_rounds = int(settings.compact_protect_rounds)
        self._preview_chars = int(settings.compact_preview_chars)
        self._min_compress_chars = int(settings.compact_min_compress_chars)
        self._summary_chars = int(settings.compact_summary_chars)
        self._l3_summary_chars = int(settings.compact_l3_summary_chars)
        self._compact_dir = Path(settings.tool_outputs_dir) / "compact"
        self._system_prompt = system_prompt
        self._tools = list(tools)
        # 主模型（与 agent 同一配置：base_url / api_key / 温度 / 思考开关全部一致），
        # 工具定义在 _summarize 调用处显式 bind（与主 agent 请求形状一致）。
        # 摘要调用加超时：端点异常时按超时降级，不阻塞 worker 的模型循环。
        model = build_chat_model(settings)
        self._model = model.model_copy(update={"request_timeout": _SUMMARY_TIMEOUT_SECONDS})

    # ──────────────────────────── L1 ────────────────────────────

    def _l1_candidates(self, messages: list[Any]) -> list[int]:
        """保护区外可压缩的长 ToolMessage 索引（对话顺序）。"""
        boundary = find_protect_boundary(messages, self._protect_rounds)
        candidates: list[int] = []
        for i, msg in enumerate(messages):
            if i >= boundary:
                break
            if not isinstance(msg, ToolMessage):
                continue
            content = msg.content
            if not isinstance(content, str):
                continue
            if _COMPACT_RE.search(content):
                continue  # 已压过，幂等跳过
            if len(content) <= self._min_compress_chars:
                continue
            candidates.append(i)
        return candidates

    def _save_full_output(self, content: str) -> str | None:
        """未落盘的输出存到 tool_outputs/compact/，返回显示路径；失败返回 None。"""
        try:
            self._compact_dir.mkdir(parents=True, exist_ok=True)
            path = self._compact_dir / f"compact_{uuid.uuid4().hex[:12]}.txt"
            path.write_text(content, encoding="utf-8", newline="\n")
            return str(path)
        except OSError:
            logger.warning("Compact L1: 输出存盘失败（仅保留预览）", exc_info=True)
            return None

    def _compact_tool_message(self, msg: ToolMessage) -> str:
        """压一条 ToolMessage：有落盘指针的截正文留指针；没有的先存盘再截。"""
        content = msg.content
        lines = content.splitlines()
        keep = _truncate_lines(lines, self._preview_chars)
        preview = "\n".join(lines[:keep])
        artifact = _ARTIFACT_RE.search(content)
        if artifact:
            pointer = artifact.group(0)
            if pointer not in preview:
                preview += f"\n{pointer}"
            note = "工具输出正文已截断，完整内容见落盘文件"
        else:
            saved = self._save_full_output(content)
            note = f"完整内容已存盘 {saved}，可用 Read 读取" if saved else "正文已截断（存盘失败）"
        return f"{_L1_MARKER}\n[Compacted L1: {note}]\n{preview}"

    def _do_l1(self, messages: list[Any], tokens_to_free: int) -> tuple[int, int]:
        """按对话顺序逐条截断，达标即停。返回 (释放 token 数, 压缩条数)。原地修改。"""
        freed = 0
        count = 0
        for i in self._l1_candidates(messages):
            if freed >= tokens_to_free:
                break
            msg = messages[i]
            new_content = self._compact_tool_message(msg)
            freed += estimate_text_tokens(msg.content) - estimate_text_tokens(new_content)
            messages[i] = msg.model_copy(update={"content": new_content})
            count += 1
        return freed, count

    # ──────────────────────────── L2 / L3 ────────────────────────────

    def _l2_start(self, messages: list[Any], boundary: int) -> int | None:
        """第一个未压缩消息的索引（跳过已有的 L2/L3 摘要）。"""
        for i in range(1, boundary):
            if not _COMPACT_RE.search(_content_str(messages[i])):
                # 不从 ToolMessage 中间起摘：把配对的 AIMessage 一起带上
                while i > 1 and isinstance(messages[i], ToolMessage):
                    i -= 1
                return i
        return None

    def _do_l2(self, messages: list[Any], current_tokens: int) -> dict[str, Any] | None:
        boundary = find_protect_boundary(messages, self._protect_rounds)
        start = self._l2_start(messages, boundary)
        if start is None:
            return None

        # 单批窗口：窗口 × 比例，且至少覆盖超额量（随窗口缩放，不写死绝对值）。
        excess = current_tokens - int(self._context_window * self._target_ratio)
        window = max(int(self._context_window * self._l2_batch_ratio), excess)
        accumulated = 0
        batch_end = start
        for i in range(start, boundary):
            accumulated += estimate_tokens([messages[i]])
            batch_end = i + 1
            if accumulated >= window:
                break
        # 未压缩范围连一个窗口都凑不满 → 单独摘它不划算，交给 L3 全量兜底。
        if accumulated < window:
            return None
        # 批次结尾不拆配对：把 trailing ToolMessage 一并带上。
        while batch_end < boundary and isinstance(messages[batch_end], ToolMessage):
            batch_end += 1

        prompt = _L2_SUMMARY_PROMPT.format(target_chars=self._summary_chars)
        summary = self._summarize(messages[start:batch_end], prompt, "L2")
        if not summary:
            return None

        summary_msg = HumanMessage(
            content=(
                f"{_L2_MARKER}\n"
                f"[Compacted L2: messages[{start}:{batch_end}] 约 {accumulated} tokens 已压缩为摘要]\n"
                f"{summary}"
            )
        )
        new_messages = messages[:start] + [summary_msg] + messages[batch_end:]
        logger.info(
            "Compact L2: 摘要 messages[%d:%d]（约 %d tokens），当前约 %d tokens",
            start, batch_end, accumulated, current_tokens,
        )
        return self._build_replacement(messages, new_messages)

    def _do_l3(self, messages: list[Any]) -> dict[str, Any] | None:
        boundary = find_protect_boundary(messages, self._protect_rounds)
        if boundary <= 1:
            return None
        unprotected = estimate_tokens(messages[1:boundary])
        if unprotected <= 0:
            return None
        prompt = _L3_SUMMARY_PROMPT.format(target_chars=self._l3_summary_chars)
        summary = self._summarize(messages[1:boundary], prompt, "L3")
        if not summary:
            return None
        summary_msg = HumanMessage(
            content=(
                f"{_L3_MARKER}\n"
                f"[Compacted L3: 全部历史 messages[1:{boundary}]（约 {unprotected} tokens）已压缩为摘要]\n"
                f"{summary}"
            )
        )
        new_messages = [messages[0], summary_msg] + messages[boundary:]
        logger.info(
            "Compact L3: 全量摘要 messages[1:%d]（约 %d tokens）", boundary, unprotected,
        )
        return self._build_replacement(messages, new_messages)

    def _summarize(self, batch: list[Any], prompt: str, label: str) -> str:
        """同步摘要调用。请求 = [系统提示词] + 历史批次 + [末尾追加的指令 HumanMessage]。

        系统提示词、模型配置、工具定义与 agent 完全一致，且批次就是主对话历史的
        前缀本身 → 本次请求的前缀与上一轮主调用完全相同，KV cache 全量命中，
        只为末尾追加的指令付增量。
        """
        # 请求里必须带 tools 且与主 agent 完全一致（system + tools + messages 前
        # 缀整体参与 cache 前缀计算），漏掉 tools 会从请求头部就开始 cache miss。
        model = self._model.bind_tools(self._tools) if self._tools else self._model
        call_messages = (
            [SystemMessage(content=self._system_prompt)]
            + list(batch)
            + [HumanMessage(content=prompt)]
        )
        try:
            response = model.invoke(call_messages)
            text = _content_str(response).strip()
            if text:
                return extract_summary(text)
        except Exception:  # noqa: BLE001 - 摘要失败不能阻塞任务，降级走兜底
            logger.warning("Compact %s: 摘要调用失败", label, exc_info=True)
        return ""

    # ──────────────────────────── 状态更新 ────────────────────────────

    @staticmethod
    def _build_replacement(original: list[Any], new_messages: list[Any]) -> dict[str, Any]:
        """整表替换：RemoveMessage 清旧 + 全新 UUID 重追加。

        ``add_messages`` reducer 对同 ID 原位更新、新 ID 追加到末尾；要在中间
        插入摘要必须先删后以全新 UUID 按序重加（对齐 deer-flow / tongxiao 实践）。
        """
        removals = [
            RemoveMessage(id=str(m.id)) for m in original if getattr(m, "id", None)
        ]
        readded = [
            m.model_copy(update={"id": str(uuid.uuid4())}) for m in new_messages
        ]
        return {"messages": removals + readded}

    # ──────────────────────────── 钩子 ────────────────────────────

    def before_model(self, state: Any, runtime: Any = None) -> dict[str, Any] | None:
        messages = list((state or {}).get("messages") or [])
        if not messages:
            return None

        current = estimate_context_tokens(messages)
        trigger = int(self._context_window * self._trigger_ratio)
        target = int(self._context_window * self._target_ratio)
        if current < trigger:
            return None
        tokens_to_free = current - target

        # lazy L1：在副本上试算，单独够用就直接应用（零 LLM 成本）。
        # 不够时不应用 L1，L2/L3 在原始消息上做语义压缩——避免先截断再摘要
        # 造成信息双重损失；L1 仅在 L2/L3 均失败时作零成本兜底。
        l1_messages = list(messages)
        freed, count = self._do_l1(l1_messages, tokens_to_free)
        if count and current - freed <= target:
            logger.info(
                "Compact L1: 截断 %d 条工具输出，释放约 %d tokens（%d → %d，目标 %d）",
                count, freed, current, current - freed, target,
            )
            return {"messages": l1_messages}

        # L2：比例窗口的批量语义摘要（渐进式，每轮一个批次）。
        l2 = self._do_l2(messages, current)
        if l2 is not None:
            return l2

        # L3：全量兜底摘要。
        l3 = self._do_l3(messages)
        if l3 is not None:
            return l3

        # L1 兜底：L2/L3 均失败（无摘要模型 / 范围不足）时回退已算好的 L1。
        if count:
            logger.info(
                "Compact L1 兜底: 截断 %d 条，释放约 %d tokens（%d → %d）",
                count, freed, current, current - freed,
            )
            return {"messages": l1_messages}
        # 无可压缩 → 交给 ContextBudgetMiddleware 折叠（最后防线）。
        return None
