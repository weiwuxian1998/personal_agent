"""任务类型注册表（唯一事实来源）。

每个 `task_kind` 挂三样东西：**系统提示词、工具集、默认运行目录（cwd）**。
新增类型 = 加一条注册项，不改 `create_session` / `build_worker` 的签名。

当前取值：
- `general`：默认，通用 worker（fs / shell / web）；
- `memory_consolidate`：记忆整理 worker（MEMORY_DESIGN §7）：与 general 同一套通用
  fs + Shell，不造专属工具；"只写 memory 区、不碰 notes/素材" 是提示词约定不是沙箱；
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
from tools.fs import build_fs_tools
from tools.shell import build_shell_tools
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
    # 按类型差异化的上下文窗口（MEMORY_DESIGN §7-3）：0 = 用全局默认
    # （settings.context_window_tokens）；装配层据此 model_copy 出中间件专用配置，
    # 不动通用 worker 的默认值。
    context_window_tokens: int = 0


# ---------------------------------------------------------------- 工具集
def _worker_tools(
    *,
    settings: Settings,
    task: TaskSpec,
    read_roots: list[Path],
    write_roots: list[Path],
) -> list[Any]:
    """general / memory_consolidate 共用的工具装配：fs + shell + web。"""
    play_root = settings.play_root_path
    tools = build_fs_tools(
        workspace_root=play_root,
        tool_outputs_dir=settings.tool_outputs_dir,
        read_roots=read_roots,
        write_roots=write_roots,
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
    # 上下文管理全被动（WorkerCompactMiddleware 自动压缩），不再暴露主动 new_context 工具。
    return tools


def _general_tools(*, settings: Settings, task: TaskSpec, redis_client: Any = None) -> list[Any]:
    # 区域模型：可玩耍区（play_root）读写删全开；Desktop/Downloads/Documents 与附件目录只读。
    return _worker_tools(
        settings=settings,
        task=task,
        read_roots=[*settings.readonly_root_paths, settings.inbox_dir],
        write_roots=[settings.play_root_path],
    )


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
        # 与 general 同一套通用工具（§7 已定方向）：PLAY_ROOT=D:\ 生效后 memory 区
        # 就在 fs 合法写路径内；写边界靠专属提示词约定，不靠工具集隔离。
        build_tools=_general_tools,
        default_cwd=lambda s: s.router_dir,
        # 大量读入 transcript → 预算提到 50w（§7-3，DeepSeek 百万窗口）。
        context_window_tokens=500_000,
        note="记忆整理 worker（MEMORY_DESIGN §7）",
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
