"""Worker agent 装配（M-WORKER 归属；端到端阶段先由主线落地最小版）。"""

from __future__ import annotations

from typing import Any

from agents.base import build_agent
from agents.middleware import ToolContextMiddleware, VisionAdapterMiddleware
from agents.prompt import worker_prompt
from config import Settings
from contracts import TaskSpec
from tools.fs import build_fs_tools
from tools.shell import build_shell_tools
from tools.web import build_web_tools


def build_worker(*, task: TaskSpec, settings: Settings, checkpointer: Any):
    workspace_root = settings.workspace_root
    tools = build_fs_tools(
        workspace_root=workspace_root,
        tool_outputs_dir=settings.tool_outputs_dir,
    )
    tools.extend(
        build_shell_tools(
            profile="worker",
            workspace_root=workspace_root,
            tool_outputs_dir=settings.tool_outputs_dir,
            default_cwd=task.cwd,
        )
    )
    tools.extend(
        build_web_tools(
            settings=settings,
            tool_outputs_dir=settings.tool_outputs_dir,
        )
    )
    return build_agent(
        settings=settings,
        tools=tools,
        system_prompt=worker_prompt(settings),
        checkpointer=checkpointer,
        middleware=[VisionAdapterMiddleware(settings), ToolContextMiddleware()],
        name="worker",
    )
