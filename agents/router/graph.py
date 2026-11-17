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
from config import Settings
from skills.registry import SkillRegistry
from tools.context import new_context
from tools.fs import build_fs_tools
from tools.memory import build_memory_router_tools
from tools.reply import build_reply_user_tool
from tools.schedule import build_schedule_tools
from tools.sessions import SessionService, build_session_tools
from tools.shell import build_shell_tools
from tools.skill import build_skill_tools


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
    # 区域模型：可玩耍区（play_root）读写删全开；Desktop/Downloads/Documents 与附件目录只读。
    play_root = settings.play_root_path
    tools.extend(
        build_fs_tools(
            workspace_root=play_root,
            tool_outputs_dir=settings.tool_outputs_dir,
            read_roots=[*settings.readonly_root_paths, settings.inbox_dir],
            write_roots=[play_root],
        )
    )
    tools.extend(
        build_shell_tools(
            profile="router",
            workspace_root=play_root,
            tool_outputs_dir=settings.tool_outputs_dir,
            default_cwd=play_root,
        )
    )
    tools.append(new_context)
    # §7.5 B1：技能说明按需加载；目录每次调用重扫，加技能 = 加文件（不需重启）。
    tools.extend(build_skill_tools(registry=SkillRegistry(settings.skills_dir), role="router"))
    # §7.5 B1：定时任务只由 router 管理；触发由 gateway 的 ScheduleScanner 投 timer 事件。
    tools.extend(build_schedule_tools(store=store))
    # §3.2 / §3.4：回读落盘记录 + 读记忆 + 工具化触发记忆整理（不做定时整理）。
    tools.extend(
        build_memory_router_tools(settings=settings, session_service=session_service)
    )
    return build_agent(
        settings=settings,
        tools=tools,
        system_prompt=router_prompt(settings),
        checkpointer=checkpointer,
        middleware=[
            # 替换式折叠：折叠时追加「折叠摘要 + 记忆快照」（仅 router；worker 关闭）。
            ContextBudgetMiddleware(settings, rehydrate_memory=True),
            # 折叠/中断留下的悬空 tool_call 与孤儿 ToolMessage 在发请求前自动修复。
            DanglingToolCallMiddleware(),
            VisionAdapterMiddleware(settings),
            ToolContextMiddleware(),
            TokenUsageReminderMiddleware(),
        ],
        name="router",
    )
