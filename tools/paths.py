"""路径解析与工作区越界守卫（M-FS）。

契约：`docs/ENGINEERING_CONTRACT.md` §7.1 / §7.3。

- 所有文件工具只允许操作 `workspace_root` 内部的路径；
- 判断前先 `Path.resolve()`（消掉 `..` 与符号链接），再用 `relative_to` 比较；
- Windows 下路径大小写不敏感，比较统一走 `os.path.normcase`；
- 越界抛 `ValueError`（`PathOutsideWorkspace` 是其子类），由工具层转成
  `error_class="path_outside_workspace"`。

本模块只依赖标准库，不 import 任何项目内模块。
"""

from __future__ import annotations

import os
from pathlib import Path

# Glob / Grep 统一跳过的目录名（契约 §7.3）
EXCLUDED_DIR_NAMES: frozenset[str] = frozenset({".git", "node_modules", ".venv", "__pycache__"})


class PathOutsideWorkspace(ValueError):
    """路径不在工作区内（契约要求越界抛 ValueError）。"""


def _norm(path: str | Path) -> str:
    """转成规范化字符串比较形式：绝对路径 + Windows 大小写归一。"""
    return os.path.normcase(os.path.abspath(str(path)))


def is_within(path: str | Path, root: str | Path) -> bool:
    """判断 `path` 是否位于 `root` 内部（含相等）；不解析符号链接。"""
    child = _norm(path)
    parent = _norm(root)
    if child == parent:
        return True
    try:
        Path(child).relative_to(Path(parent))
    except ValueError:
        return False
    return True


def resolve_in_workspace(path: str | Path, root: str | Path) -> Path:
    """把入参解析成工作区内的绝对路径，越界抛 `PathOutsideWorkspace`。

    规则：
    - 相对路径按 `root` 解析（模型常偷懒传相对路径，这里做兼容）；
    - `~` 会展开；随后 `resolve()` 消掉 `..` 与符号链接；
    - 解析结果必须是 `root` 自身或其后代，否则抛异常。

    不做存在性检查——是否存在的语义由各工具决定。
    """
    if path is None:
        raise TypeError("path 不能为空")
    root_path = Path(root).expanduser().resolve()
    raw = Path(str(path)).expanduser()
    candidate = raw if raw.is_absolute() else (root_path / raw)
    try:
        resolved = candidate.resolve()
    except OSError as exc:  # 极长路径 / 非法字符等
        raise ValueError(f"无法解析路径 {path!r}: {exc}") from exc
    if not is_within(resolved, root_path):
        raise PathOutsideWorkspace(f"路径越界: {path} 不在工作区 {root_path} 内")
    return resolved


def relative_display(path: str | Path, root: str | Path) -> str:
    """工作区内相对路径（正斜杠，便于跨平台展示）；越界时退回绝对路径。"""
    resolved = Path(path)
    try:
        rel = resolved.resolve().relative_to(Path(root).resolve())
    except (ValueError, OSError):
        return str(resolved)
    return rel.as_posix()


def has_excluded_part(path: str | Path, root: str | Path) -> bool:
    """路径（相对工作区）的任一段命中排除目录名时返回 True。"""
    try:
        rel = Path(path).resolve().relative_to(Path(root).resolve())
    except (ValueError, OSError):
        return False
    return any(part in EXCLUDED_DIR_NAMES for part in rel.parts)
