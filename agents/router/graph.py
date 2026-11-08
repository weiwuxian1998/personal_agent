"""Router agent 装配（M-ROUTER 归属；端到端阶段先由主线落地最小版）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agents.base import build_agent
from agents.middleware import VisionAdapterMiddleware
from agents.prompt import router_prompt
from config import Settings
from tools.reply import build_reply_user_tool
from tools.sessions import SessionService, build_session_tools


def build_router(
    *,
    settings: Settings,
    channel: Any,
    redis_client: Any,
    session_service: SessionService,
    checkpointer: Any,
    repo_root: Path,
):
    tools = [build_reply_user_tool(channel=channel, redis_client=redis_client)]
    tools.extend(build_session_tools(session_service))
    return build_agent(
        settings=settings,
        tools=tools,
        system_prompt=router_prompt(settings),
        checkpointer=checkpointer,
        middleware=[VisionAdapterMiddleware(settings)],
        name="router",
    )
