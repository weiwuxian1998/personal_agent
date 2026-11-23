"""Agent middleware（契约 §8.3，顺序固定）。

v1 目前落地了两项：

- `VisionAdapterMiddleware`（§8.3-3）：工具（如 `Read`）返回 `image_url` 内容块，
  而文本模型收到图片块会 400，必须由装配层按模型能力降级，工具层不判断模型能力（契约 §14-18）。
- `ToolContextMiddleware`（§8.3-10）：工具执行前把 `tool_call["id"]` 写进
  `current_tool_call_id`，保证落盘文件名与 trace 可关联。
- `DanglingToolCallMiddleware`：悬空 tool_call 补合成结果、孤儿 ToolMessage
  丢弃、配对重新编组（对齐 deer-flow `DanglingToolCallMiddleware`，
  `wrap_model_call` 只改请求不写 state）。

其余 middleware（Trace / ToolPolicy / ShellGuard / StopInjection / WorkerInbox /
ErrorNormalize）尚未实现，属于批次 B/C；实现时必须严格按
§8.3 的顺序插入，不要改变已有序号。

"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage

from agents.context import current_tool_call_id, current_trace_id
from agents.context_budget import compute_cut, estimate_tokens, truncate_oversized_tool_messages
from agents.vision import adapt_messages, inject_image_blocks, is_vision_image_context
from config import Settings

logger = logging.getLogger("agents.middleware")


def _is_multimodal_timeout(exc: BaseException) -> bool:
    """判断模型调用异常是否为 DeepSeek 多模态图片下载超时。

    这类错误来自 MaaS 代理（如 qianwenaiapi.com）的临时故障：
    服务端接收到 base64 data URL 后，内部图片预处理超时 → 400。
    临时跳过图片注入、让 agent 跑完这条消息即可，下一轮自动恢复。
    """
    msg = str(exc).lower()
    return (
        "multimodal" in msg
        and ("download failure" in msg or "timeout" in msg)
    )


# 跨节点信号必须用**模块级**状态：实测 LangGraph 每个节点在独立的 context 副本里
# 执行，ContextVar 在「工具节点 set → 模型节点 get」之间不可见。
# router/worker 都是单进程串行跑一轮，模块级状态足够安全。
# - pending_compact：本轮折叠摘要，main_router 取走后追加 JSONL 记账。
# - last_boundary：最近一次折叠的边界消息 id（观测用）。
_FOLD_STATE: dict[str, Any] = {
    "pending_compact": None,
    "last_boundary": "",
}


def take_pending_compact() -> dict[str, Any] | None:
    """取走本轮折叠信号（只返回一次）；main_router 用它追加 `compact` 记账。"""
    pending = _FOLD_STATE["pending_compact"]
    _FOLD_STATE["pending_compact"] = None
    return pending


# 工具调用实测耗时（tool_call_id → ms）：由 `ToolContextMiddleware` 在每次工具执行后写入，
# 供 router 写 turns.jsonl 的 `tool_calls[].ms`。进程内串行跑一轮，模块级字典足够。
_TOOL_MS: dict[str, int] = {}


def take_tool_ms() -> dict[str, int]:
    """取走本轮的工具有效耗时（ms），并清空；非 router 调用方可以忽略。"""
    data = dict(_TOOL_MS)
    _TOOL_MS.clear()
    return data


def reset_tool_ms() -> None:
    """丢弃残留耗时记录（每轮开始前调用，避免上一轮的 id 串到本轮）。"""
    _TOOL_MS.clear()


class VisionAdapterMiddleware(AgentMiddleware):
    """图片引用还原 / 降级（§8.3-3，模式对齐 deer-flow `ViewImageMiddleware`）。

    工具层（fs.Read）只回 `[图片] <路径> | <mime> | <n> 字节` 引用文本，
    **从不内联 base64**——base64 会随 ToolMessage 进 checkpoint / 日志，把存储撑大几十倍。

    - `llm_supports_vision=True`（主 agent 模型，GLM 系支持视觉）：
      每次模型调用前扫描消息里的图片引用，把最近 ≤6 张（按路径去重）还原成
      一条带标记的 user 消息追加到请求末尾（端点限制：图片只能出现在 user 消息，
      tool role 带图 400）；
    - `False`：把 image 块换成文本占位（`adapt_messages`），兼容历史 checkpoint
      里残留的旧内联格式。

    注入消息带 ID 前缀 + additional_kwargs 双重标记，每次请求先剥离旧副本再重建：
    `request.override` 只影响本次请求、不写回 state，正常不会产生副本，但防御
    历史/异常 checkpoint 里的搁浅副本永远骑在后续请求上。

    **只能在 `wrap_model_call` 做**：改用 `before_model`/`after_model` 会把 base64
    写进 state，中断的 run 会把它永久留在 checkpoint 里（deer-flow #4267 的教训）。
    """

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._supports_vision = bool(settings.llm_supports_vision)

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        messages = [m for m in request.messages if not is_vision_image_context(m)]
        dropped = len(messages) != len(request.messages)
        if self._supports_vision:
            try:
                return handler(request.override(messages=inject_image_blocks(messages)))
            except Exception as exc:
                if _is_multimodal_timeout(exc):
                    logger.warning(
                        "multimodal timeout → fallback without images (supports_vision=False 本轮)"
                    )
                    if dropped:
                        return handler(request.override(messages=messages))
                    return handler(
                        request.override(
                            messages=adapt_messages(
                                request.messages, supports_vision=False
                            )
                        )
                    )
                raise
        if dropped:
            return handler(request.override(messages=messages))
        messages = adapt_messages(request.messages, supports_vision=False)
        return handler(request.override(messages=messages))


class ToolContextMiddleware(AgentMiddleware):
    """把模型产出的 `tool_call["id"]` 写进 `current_tool_call_id`（契约 §7.7 / §8.3-10）。

    落盘路径是 `tool_outputs/<trace_id>/<tool_call_id>-<n>.md`。没有这一步，
    `tools/web.py` / `tools/shell.py` 拿到的 `current_tool_call_id` 为空，文件名退化为
    随机 ULID，trace 与落盘文件无法关联（`scripts/check_web.py` 里手工 set 过，
    所以单测能过、真实图里却丢关联——必须由装配层统一赋值）。

    用 `wrap_tool_call`：调用前 set、调用后 reset，不污染同一轮里的下一个工具。
    """

    def wrap_tool_call(self, request: Any, handler: Any) -> Any:
        call = getattr(request, "tool_call", None) or {}
        call_id = str(call.get("id") or "") if isinstance(call, dict) else str(getattr(call, "id", "") or "")
        if not call_id:
            return handler(request)
        token = current_tool_call_id.set(call_id)
        started = time.monotonic()
        try:
            return handler(request)
        finally:
            current_tool_call_id.reset(token)
            # 实测单次工具耗时（含工具自身执行，不含模型思考），供 turns.jsonl 记账。
            _TOOL_MS[call_id] = int((time.monotonic() - started) * 1000)


class ContextBudgetMiddleware(AgentMiddleware):
    """上下文预算与折叠（设计文档 §3.6）。

    - soft（256k）触发折叠，hard（320k）在折叠后仍超限时截断窗口内的长工具输出；
    - 折叠 = 逐条 `RemoveMessage`（**不是** REMOVE_ALL），保留最近 8 条 /
      40k tokens、最后一条 HumanMessage，且不拆 `tool_calls` 与 `ToolMessage`；
    - 每次折叠把 `boundary_checkpoint_id`（被删的最后一条消息 id）写进
      `memory/context-boundaries.jsonl`，供后续对账。

    `new_context` 主动折叠与「记忆快照替换注入」均已删除（MEMORY_DESIGN §8.2 /
    任务 #7：facts/decisions/summary 废弃）：router 白天不做任何主动裁剪，
    夜间靠「时间戳折叠 + 记忆注入」（main_router._apply_nightly_fold）刷新；
    本中间件只剩被动安全网职责，worker 与 router 行为一致。
    """

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._soft = int(settings.context_soft_tokens)
        self._hard = int(settings.context_hard_tokens)
        self._keep_recent = int(settings.context_keep_recent_messages)
        self._keep_tokens = int(settings.context_keep_recent_tokens)
        self._tool_max_chars = int(settings.context_tool_message_max_chars)
        self._memory_dir = Path(settings.memory_dir)
        self._boundary_path = self._memory_dir / "context-boundaries.jsonl"

    # ------------------------------------------------------------------ 内部
    def _record_boundary(
        self,
        boundary: str,
        removed: int,
        tokens_before: int,
        tokens_after: int,
        *,
        reason: str,
    ) -> None:
        _FOLD_STATE["last_boundary"] = boundary
        _FOLD_STATE["pending_compact"] = {
            "reason": reason,
            "boundary": boundary,
            "removed": removed,
            "tokens_before": tokens_before,
            "tokens_after": tokens_after,
        }
        try:
            self._boundary_path.parent.mkdir(parents=True, exist_ok=True)
            record = {
                "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                "trace_id": current_trace_id.get() or "",
                "boundary_checkpoint_id": boundary,
                "reason": reason,
                "removed_messages": removed,
                "tokens_before": tokens_before,
                "tokens_after": tokens_after,
            }
            with open(self._boundary_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001 - 记录失败不能影响主流程
            logger.warning("写 context-boundaries.jsonl 失败", exc_info=True)

    # ------------------------------------------------------------------ 钩子
    def before_model(self, state: Any, runtime: Any = None) -> dict[str, Any] | None:
        messages = list((state or {}).get("messages") or [])
        if not messages:
            return None
        tokens_before = estimate_tokens(messages)
        if tokens_before < self._soft:
            return None

        cut = compute_cut(
            messages,
            keep_recent=self._keep_recent,
            keep_recent_tokens=self._keep_tokens,
        )
        if cut <= 0:
            return None

        removed = messages[:cut]
        kept = list(messages[cut:])
        truncated = 0
        if estimate_tokens(kept) > self._hard:
            truncated = truncate_oversized_tool_messages(
                kept,
                max_chars=self._tool_max_chars,
            )

        removals = [
            RemoveMessage(id=str(m.id))
            for m in removed
            if getattr(m, "id", None)
        ]
        if not removals:
            return None

        boundary = str(getattr(removed[-1], "id", ""))
        tokens_after = estimate_tokens(messages[cut:])
        reason = f"budget_soft_{self._soft // 1000}k"
        if truncated:
            reason += "+tool_truncate"
        self._record_boundary(boundary, len(removed), tokens_before, tokens_after, reason=reason)
        logger.info(
            "上下文折叠：删除 %d 条（soft=%d hard=%d tokens %d→%d，截断 %d 条工具输出）boundary=%s",
            len(removed), self._soft, self._hard, tokens_before, tokens_after, truncated, boundary,
        )
        return {"messages": removals}


_STOP_WRAP_UP = (
    "用户已要求停止当前任务。不要再发起任何新的工具调用（已经在跑的工具会让它跑完）。"
    "请用三句话收尾：1) 已经完成了什么；2) 还剩什么没做；3) 建议用户下一步怎么办。"
)


class WorkerHeartbeatMiddleware(AgentMiddleware):
    """worker 每轮模型调用写一次 `sessions.last_seen`（§5.7.3）。

    巡检（gateway 的 `TaskWatchdog`）用 `last_seen` 判停滞；写失败只告警，
    绝不影响任务本身。
    """

    def __init__(self, on_round: Any) -> None:
        super().__init__()
        self._on_round = on_round

    def before_model(self, state: Any, runtime: Any = None) -> None:
        try:
            self._on_round()
        except Exception:  # noqa: BLE001 - 心跳失败不能影响任务
            logger.warning("worker 心跳写入失败", exc_info=True)
        return None


class WorkerInboxMiddleware(AgentMiddleware):
    """把 router 的 `send_message_to_session` 指令注入 worker（§5.6）。

    指令来自 `WorkerControl` 的后台线程（消费 `asst:stream:worker:<task_id>`），
    在每次模型调用前一次性取走，避免同一条指令重复注入。
    """

    def __init__(self, control: Any) -> None:
        super().__init__()
        self._control = control

    def before_model(self, state: Any, runtime: Any = None) -> dict[str, Any] | None:
        items = self._control.drain_instructions()
        if not items:
            return None
        return {
            "messages": [
                HumanMessage(
                    content=(
                        "[来自 router 的追加指令，先判断是否与当前任务冲突；冲突时以用户最新意图为准]\n"
                        f"{text}"
                    )
                )
                for text in items
            ]
        }


class StopInjectionMiddleware(AgentMiddleware):
    """三级终止里的第 1–2 级（§5.5）。

    - 看到停止标记后，下一轮模型调用前注入一次收尾指令（第 2 级），
      并回写 `stop_key=2`，router 的 `StopCommand` 据此不再升级；
    - **之后**所有新工具调用一律拒绝（返回 ToolMessage），已在执行的工具不受影响；
    - 第 3 级（`taskkill /F /T`）由 router 的 `StopCommand` 在"已礼仍不停"时执行，
      worker 自己不做自杀。
    """

    def __init__(self, control: Any) -> None:
        super().__init__()
        self._control = control

    def before_model(self, state: Any, runtime: Any = None) -> dict[str, Any] | None:
        if not self._control.stop_requested() or self._control.injected():
            return None
        self._control.mark_injected()
        return {"messages": [HumanMessage(content=_STOP_WRAP_UP)]}

    def wrap_tool_call(self, request: Any, handler: Any) -> Any:
        if not self._control.stop_requested():
            return handler(request)
        call = getattr(request, "tool_call", None) or {}
        call_id = str(call.get("id") or "") if isinstance(call, dict) else ""
        return ToolMessage(
            content="已收到停止请求：不再执行新的工具调用，请直接给出收尾结论。",
            tool_call_id=call_id,
        )


_TOKEN_REMINDER_FLAG = "token_usage_reminder"


def _render_usage_reminder(usage: dict[str, Any]) -> str:
    """把 langchain 标准 usage_metadata 渲染成 `<system_reminder type='token_count'>` 块。

    reasoning tokens 在 `output_token_details.reasoning`（langchain-openai 会丢
    `reasoning_content` 文本，但 usage 里的 reasoning token 数保留，契约 §14-38）。
    """
    output_details = usage.get("output_token_details") or {}
    input_details = usage.get("input_token_details") or {}
    lines = [
        f"input_tokens={usage.get('input_tokens', 0)}",
        f"output_tokens={usage.get('output_tokens', 0)}",
        f"total_tokens={usage.get('total_tokens', 0)}",
    ]
    if output_details.get("reasoning"):
        lines.append(f"reasoning_tokens={output_details['reasoning']}")
    if input_details.get("cache_read"):
        lines.append(f"cache_read_tokens={input_details['cache_read']}")
    return (
        "<system_reminder type='token_count'>\n"
        "上一轮模型调用的 token 用量：\n"
        + "\n".join(lines)
        + "\n</system_reminder>"
    )


# --------------------------------------------------------------------- 悬空 tool_call 修复
_SYNTHETIC_INTERRUPTED = "[工具调用被中断，未返回结果。请基于已有信息继续，不要重复调用同一工具。]"


def _call_id(tool_call: Any) -> str:
    """取 tool_call 的 id（兼容 dict 与对象两种形态），无效返回空串。"""
    value = tool_call.get("id") if isinstance(tool_call, dict) else getattr(tool_call, "id", None)
    return str(value) if value else ""


def _call_name(tool_call: Any) -> str:
    value = tool_call.get("name") if isinstance(tool_call, dict) else getattr(tool_call, "name", None)
    return str(value) if value else "unknown"


def build_paired_messages(messages: list[Any]) -> list[Any] | None:
    """修复悬空 tool_call / 孤儿 ToolMessage（对齐 deer-flow `DanglingToolCallMiddleware`）。

    两类坏历史都会让严格的 OpenAI 兼容端点 400：

    1. **悬空 tool_call**：AIMessage 带着 tool_calls 但没有对应 ToolMessage
       （工具执行中途崩溃 / 用户打断 / checkpoint 落盘不完整）；
    2. **孤儿 ToolMessage**：`tool_call_id` 在任何 AIMessage 的 tool_calls 里
       都找不到（上游 AIMessage 被折叠 / 摘要删掉）。

    处理规则（deer-flow 已验证的形状）：

    - ToolMessage 一律**重新编组**到它的 AIMessage 之后（同 id 按文档顺序
      FIFO 配对）——中间被插话/新轮次打断的配对恢复相邻；
    - 悬空 tool_call 就地补一条 `status="error"` 的合成 ToolMessage，模型看到
      可恢复的错误而不是 400；
    - 孤儿 ToolMessage 直接丢弃（上游调用已不存在，留着只会被拒）。

    返回 `None` 表示历史本来就合法（调用方跳过 override，零开销）。
    """
    results_by_id: dict[str, deque[Any]] = defaultdict(deque)
    for message in messages:
        if isinstance(message, ToolMessage):
            results_by_id[str(message.tool_call_id)].append(message)

    patched: list[Any] = []
    for message in messages:
        if isinstance(message, ToolMessage):
            # 不在原位发出：配对结果由其 AIMessage 统一携带（孤儿因此被丢弃）。
            continue

        patched.append(message)
        if not isinstance(message, AIMessage):
            continue
        for tool_call in getattr(message, "tool_calls", None) or []:
            call_id = _call_id(tool_call)
            if not call_id:
                continue
            queue = results_by_id.get(call_id)
            if queue:
                patched.append(queue.popleft())
            else:
                patched.append(
                    ToolMessage(
                        content=_SYNTHETIC_INTERRUPTED,
                        tool_call_id=call_id,
                        name=_call_name(tool_call),
                        status="error",
                    )
                )

    if patched == messages:
        return None
    return patched


class DanglingToolCallMiddleware(AgentMiddleware):
    """模型调用前自动修复悬空 tool_call / 孤儿 ToolMessage（deer-flow 同款）。

    用 `wrap_model_call` + `request.override`（与 VisionAdapterMiddleware 同一
    策略）：合成/编组只影响**本次请求**，不写回 graph state，checkpoint 历史
    不会被合成消息污染。这也是 `context_budget._safe_cut` 可以不做孤儿防御的
    前提——折叠窗口哪怕切出坏边界，这里会在发请求前自动修好。
    """

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        patched = build_paired_messages(list(request.messages or []))
        if patched is not None:
            return handler(request.override(messages=patched))
        return handler(request)


class TokenUsageReminderMiddleware(AgentMiddleware):
    """每轮模型调用前，把最近一次 LLM usage 以 user 消息注入（参考 deer-flow 格式）。

    用 `wrap_model_call` + `request.override`：注入只影响**本次请求**，
    不写回 graph state（与 VisionAdapterMiddleware 同一策略），checkpoint 历史
    不会被 reminder 消息污染；每轮都基于最新 usage 重新生成。
    """

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        usage: dict[str, Any] | None = None
        for message in reversed(list(request.messages or [])):
            usage = getattr(message, "usage_metadata", None)
            if usage:
                break
        if not usage:
            # 本线程还没有任何模型产出（第一轮），无可注入。
            return handler(request)
        reminder = HumanMessage(content=_render_usage_reminder(usage))
        reminder.additional_kwargs[_TOKEN_REMINDER_FLAG] = True
        messages = list(request.messages) + [reminder]
        return handler(request.override(messages=messages))


