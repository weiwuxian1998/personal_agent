"""任务类型注册表（唯一事实来源）。

每个 `task_kind` 挂三样东西：**系统提示词、工具集、默认运行目录（cwd）**。
新增类型 = 加一条注册项，不改 `create_session` / `build_worker` 的签名。

当前取值：
- `general`：默认，通用 worker（fs / shell / web）；
- `memory_consolidate`：记忆整理 worker，只读写 `memory/` 目录；
- `cua`：纯视觉 Computer Use worker（§8）：12 个动作原语 + `act` 批次入口，
  不复用 `general` 的 fs/shell 工具集；坐标只认截图像素，不出现 ref/DOM。`cua` 包
  依赖 Windows DLL，因此走惰性导入。

提示词文本仍归 `agents/prompt.py`（另一个会话维护）；本文件只引用函数，不改内容。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from agents.prompt import worker_prompt
from config import Settings
from contracts import TaskSpec
from skills.registry import SkillRegistry
from tools.context import new_context
from tools.fs import build_fs_tools
from tools.memory import build_memory_worker_tools
from tools.shell import build_shell_tools
from tools.skill import build_skill_tools
from tools.web import build_web_tools

ToolBuilder = Callable[..., list[Any]]


class TaskKindError(ValueError):
    """任务类型不可用；`error_class` 直接作为工具返回值里的分类。"""

    error_class = "task_kind_invalid"


class UnknownTaskKind(TaskKindError):
    error_class = "task_kind_unknown"


class TaskKindNotImplemented(TaskKindError):
    error_class = "task_kind_not_implemented"


@dataclass(frozen=True)
class TaskKindSpec:
    kind: str
    system_prompt: Callable[[Settings], str]
    build_tools: ToolBuilder
    default_cwd: Callable[[Settings], Path]
    implemented: bool = True
    note: str = ""


# ---------------------------------------------------------------- 工具集
def _general_tools(*, settings: Settings, task: TaskSpec, redis_client: Any = None) -> list[Any]:
    # 区域模型：可玩耍区（play_root）读写删全开；Desktop/Downloads/Documents 与附件目录只读。
    play_root = settings.play_root_path
    tools = build_fs_tools(
        workspace_root=play_root,
        tool_outputs_dir=settings.tool_outputs_dir,
        read_roots=[*settings.readonly_root_paths, settings.inbox_dir],
        write_roots=[play_root],
    )
    tools.extend(
        build_shell_tools(
            profile="worker",
            workspace_root=play_root,
            tool_outputs_dir=settings.tool_outputs_dir,
            default_cwd=task.cwd,
        )
    )
    tools.extend(
        build_web_tools(
            settings=settings,
            tool_outputs_dir=settings.tool_outputs_dir,
            # 契约 §7.7：worker 侧更保守，避免工具输出撑爆上下文（旧实测 40 次搜索累计 652 KB）
            default_count=settings.worker_web_default_count,
            default_need_content=False,
            max_content_chars=settings.worker_web_content_chars,
        )
    )
    tools.append(new_context)
    # §7.5 B1：worker 也能按需加载技能（目录每次调用重扫，加技能 = 加文件）。
    tools.extend(build_skill_tools(registry=SkillRegistry(settings.skills_dir), role="worker"))
    return tools


def _memory_tools(*, settings: Settings, task: TaskSpec, redis_client: Any = None) -> list[Any]:
    # 通用 fs 工具受 workspace_root 限制，够不到 memory/ 目录，这里换成专用工具。
    return build_memory_worker_tools(settings)


def _memory_prompt(settings: Settings) -> str:
    """`memory_consolidate` 专属系统提示词（占位挂点）。

    正文由提示词会话写在 `agents/prompt.py`，约定函数名 `memory_worker_prompt(settings)`；
    这里只做「按类型取不同 prompt」的解析。函数尚未落地时退回通用 worker 提示词，
    保证注册表不因跨会话改动而导入失败。
    """
    from agents import prompt as prompt_module

    dedicated = getattr(prompt_module, "memory_worker_prompt", None)
    return dedicated(settings) if callable(dedicated) else worker_prompt(settings)


# ---------------------------------------------------------------- 注册表
def _cua_tools(*, settings: Settings, task: TaskSpec, redis_client: Any = None) -> list[Any]:
    # 惰性导入：cua 包会加载 user32/gdi32，非 cua 任务不应为此付出代价。
    from cua.tools import build_cua_tools

    return build_cua_tools(settings=settings, task=task)


def _cua_prompt(settings: Settings) -> str:
    """`cua` 专属系统提示词；正文在 `agents/prompt.py`（`cua_worker_prompt`）。"""
    from agents import prompt as prompt_module

    dedicated = getattr(prompt_module, "cua_worker_prompt", None)
    return dedicated(settings) if callable(dedicated) else worker_prompt(settings)


_REGISTRY: dict[str, TaskKindSpec] = {
    "general": TaskKindSpec(
        kind="general",
        system_prompt=worker_prompt,
        build_tools=_general_tools,
        default_cwd=lambda s: s.play_root_path,
    ),
    "memory_consolidate": TaskKindSpec(
        kind="memory_consolidate",
        system_prompt=_memory_prompt,
        build_tools=_memory_tools,
        default_cwd=lambda s: s.router_dir,
        note="记忆整理 worker（设计 §3.4 / D8）",
    ),
    "cua": TaskKindSpec(
        kind="cua",
        system_prompt=_cua_prompt,
        build_tools=_cua_tools,
        default_cwd=lambda s: s.workspace_root,
        note="纯视觉 CUA worker（§8：单屏、12 原语 + act）",
    ),
}


def available_task_kinds() -> list[str]:
    """当前可派发的类型（不含只注册未实现的）。"""
    return [kind for kind, spec in _REGISTRY.items() if spec.implemented]


def _available_text() -> str:
    return "、".join(available_task_kinds())


def resolve_task_kind(kind: str) -> TaskKindSpec:
    """按字符串取注册项；未知/未实现都显式抛错，不做静默回落。"""
    key = (kind or "general").strip() or "general"
    spec = _REGISTRY.get(key)
    if spec is None:
        raise UnknownTaskKind(f'未知的任务类型 "{key}"；当前可用类型: {_available_text()}')
    if not spec.implemented:
        raise TaskKindNotImplemented(
            f'任务类型 "{key}" 尚未实现（{spec.note}）；当前可用类型: {_available_text()}'
        )
    return spec
