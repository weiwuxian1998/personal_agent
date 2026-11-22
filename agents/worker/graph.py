"""Worker agent 装配（M-WORKER 归属；端到端阶段先由主线落地最小版）。

任务类型（`general` / `memory_consolidate` / `cua`）的差异——系统提示词、工具集、
默认运行目录、上下文窗口预算——统一由 `agents/task_kinds.py` 的注册表提供；
本文件只做装配，不再按类型写 if 分支。
"""

from __future__ import annotations

from typing import Any

from agents.base import build_agent
from agents.compact import WorkerCompactMiddleware
from agents.middleware import (
    ContextBudgetMiddleware,
    DanglingToolCallMiddleware,
    StopInjectionMiddleware,
    TokenUsageReminderMiddleware,
    ToolContextMiddleware,
    VisionAdapterMiddleware,
    WorkerHeartbeatMiddleware,
    WorkerInboxMiddleware,
)
from agents.task_kinds import resolve_task_kind
from config import Settings
from contracts import TaskSpec
from tools.worker_messages import build_send_message_tool


def build_worker(
    *,
    task: TaskSpec,
    settings: Settings,
    checkpointer: Any,
    control: Any = None,
    redis_client: Any = None,
    on_round: Any = None,
):
    # 未实现/未知类型在这里显式抛错（create_session 已提前拦过一次）。
    spec = resolve_task_kind(task.task_kind)
    tools = spec.build_tools(settings=settings, task=task, redis_client=redis_client)
    if redis_client is not None:
        tools.append(build_send_message_tool(redis_client=redis_client, task=task))
    # 上下文窗口按类型差异化（MEMORY_DESIGN §7-3）：注册表给了覆盖值就 model_copy
    # 出压缩中间件专用配置，通用 worker 不动全局默认；仍然是数据驱动，不写 if 分支。
    compact_settings = settings
    if spec.context_window_tokens and spec.context_window_tokens != settings.context_window_tokens:
        compact_settings = settings.model_copy(
            update={"context_window_tokens": spec.context_window_tokens}
        )
    middleware: list[Any] = [
        # 三级压缩（L1 截断 / L2 批量摘要 / L3 全量摘要）必须排在折叠之前：
        # 压缩把用量降到 target 后，ContextBudget 的 soft 阈值不会再触发，
        # 折叠降级为压缩彻底失败时的最后防线。
        WorkerCompactMiddleware(
            compact_settings,
            system_prompt=spec.system_prompt(settings),
            tools=tools,
        ),
        ContextBudgetMiddleware(settings),
        # 折叠/中断留下的悬空 tool_call 与孤儿 ToolMessage 在发请求前自动修复。
        DanglingToolCallMiddleware(),
        VisionAdapterMiddleware(settings),
    ]
    if on_round is not None:
        middleware.append(WorkerHeartbeatMiddleware(on_round))
    if control is not None:
        # 顺序按 §8.3：StopInjection（6）→ WorkerInbox（7），最后才是 ToolContext（10）。
        middleware.extend([StopInjectionMiddleware(control), WorkerInboxMiddleware(control)])
    middleware.append(ToolContextMiddleware())
    middleware.append(TokenUsageReminderMiddleware())
    return build_agent(
        settings=settings,
        tools=tools,
        system_prompt=spec.system_prompt(settings),
        checkpointer=checkpointer,
        middleware=middleware,
        name="worker",
    )
