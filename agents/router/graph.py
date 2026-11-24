"""Router agent 装配（M-ROUTER 归属；端到端阶段先由主线落地最小版）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agents.base import build_agent
from agents.middleware import (
    ContextBudgetMiddleware,
    DanglingToolCallMiddleware,
    TokenUsageReminderMiddleware,
    ToolContextMiddleware,
    VisionAdapterMiddleware,
)
from agents.prompt import router_prompt
from agents.transcript import TranscriptMiddleware
from config import Settings
from tools.fs import build_fs_tools
from tools.memory import build_memory_router_tools
from tools.reply import build_reply_user_tool
from tools.schedule import build_schedule_tools
from tools.sessions import SessionService, build_session_tools
from tools.shell import build_shell_tools


def build_router(
    *,
    settings: Settings,
    outbox: Any,
    redis_client: Any,
    session_service: SessionService,
    checkpointer: Any,
    store: Any,
):
    tools = [build_reply_user_tool(bus=outbox, redis_client=redis_client)]
    tools.extend(build_session_tools(session_service))
    # 区域模型：workspace_root（可玩耍区）读写删全开；Desktop/Downloads/Documents 与附件目录只读。
    workspace = settings.workspace_root
    tools.extend(
        build_fs_tools(
            workspace_root=workspace,
            tool_outputs_dir=settings.tool_outputs_dir,
            read_roots=[*settings.readonly_root_paths, settings.inbox_dir],
            write_roots=[workspace],
        )
    )
    tools.extend(
        build_shell_tools(
            profile="router",
            workspace_root=workspace,
            tool_outputs_dir=settings.tool_outputs_dir,
            default_cwd=workspace,
        )
    )
    # 上下文管理全被动（压缩/折叠由中间件自动处理），不再暴露主动 new_context 工具。
    # §7.5 B1：定时任务只由 router 管理；触发由 gateway 的 ScheduleScanner 投 timer 事件。
    tools.extend(build_schedule_tools(store=store))
    # §3.2：回读落盘记录（记忆本体 router 用通用 Read 读 index.md / threads）。
    # 派发记忆整理统一走 create_session(task_kind="memory_consolidate")，没有专属工具。
    tools.extend(build_memory_router_tools(settings=settings))
    return build_agent(
        settings=settings,
        tools=tools,
        system_prompt=router_prompt(settings),
        checkpointer=checkpointer,
        middleware=[
            # transcript 按天流水 + created_at 打标（MEMORY_DESIGN §4/§8.2）：排最前，
            # 保证折叠/注入发生的同一轮里消息已带时间戳、已入流水。
            TranscriptMiddleware(settings),
            # 被动安全网：白天预算折叠；夜间刷新走 _apply_nightly_fold（§8.2）。
            ContextBudgetMiddleware(settings),
            # 折叠/中断留下的悬空 tool_call 与孤儿 ToolMessage 在发请求前自动修复。
            DanglingToolCallMiddleware(),
            VisionAdapterMiddleware(settings),
            ToolContextMiddleware(),
            TokenUsageReminderMiddleware(),
        ],
        name="router",
    )
