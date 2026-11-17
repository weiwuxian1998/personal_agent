"""技能注册表（契约 §7.5 B1）。

一个技能 = 一个带 YAML frontmatter 的 Markdown 文件：

    ---
    name: pdf-report
    description: 把调研结果整理成 PDF 报告
    scope: both            # router | worker | both
    ---
    正文（模型按需读取的完整说明）

设计约束：
- 只用标准库解析 frontmatter（`key: value` 子集），不引入 PyYAML；
- 每次调用重新扫描目录：加技能 = 加文件，不需要重启或改注册表代码；
- frontmatter 缺失/字段不全/scope 非法的文件**跳过并告警**，不影响其它技能；
- 同名技能取先扫描到的一份（按路径排序，结果稳定）。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

Scope = Literal["router", "worker", "both"]
_VALID_SCOPES: frozenset[str] = frozenset({"router", "worker", "both"})

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.S)
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


@dataclass(frozen=True)
class SkillCard:
    """技能卡片：`Skill()` 无参时返回的元信息。"""

    name: str
    description: str
    scope: Scope
    path: str


def parse_skill(path: Path) -> tuple[SkillCard, str] | None:
    """解析单个技能文件；格式不合法返回 None（不抛异常）。"""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("技能文件读取失败 path=%s err=%s", path, exc)
        return None
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        logger.warning("技能文件缺少 frontmatter，已跳过 path=%s", path)
        return None

    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, sep, value = stripped.partition(":")
        if not sep:
            continue
        meta[key.strip().lower()] = value.strip().strip("\"'")

    name = meta.get("name", "")
    description = meta.get("description", "")
    scope = meta.get("scope", "")
    if not _NAME_RE.match(name):
        logger.warning("技能 name 不合法（需小写字母/数字/-_.）path=%s name=%r", path, name)
        return None
    if not description:
        logger.warning("技能缺少 description，已跳过 path=%s", path)
        return None
    if scope not in _VALID_SCOPES:
        logger.warning("技能 scope 不合法（router/worker/both）path=%s scope=%r", path, scope)
        return None

    card = SkillCard(name=name, description=description, scope=scope, path=str(path))  # type: ignore[arg-type]
    return card, text[match.end():]


class SkillRegistry:
    """扫描技能目录，按角色过滤并读取正文。"""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def _scan(self) -> list[tuple[SkillCard, str]]:
        if not self.root.exists():
            return []
        found: list[tuple[SkillCard, str]] = []
        seen: set[str] = set()
        for path in sorted(p for p in self.root.rglob("*.md") if p.is_file()):
            parsed = parse_skill(path)
            if parsed is None:
                continue
            card, body = parsed
            if card.name in seen:
                logger.warning("技能重名，忽略后出现的 path=%s name=%s", path, card.name)
                continue
            seen.add(card.name)
            found.append((card, body))
        return found

    def cards(self) -> list[SkillCard]:
        return [card for card, _ in self._scan()]

    def list_for(self, role: str) -> list[SkillCard]:
        """该角色可见的技能（`both` 对所有角色可见）。"""
        return [card for card in self.cards() if card.scope in ("both", role)]

    def load(self, name: str, role: str) -> str:
        """读取技能正文；不存在或越 scope 抛 `ValueError`。"""
        for card, body in self._scan():
            if card.name != name:
                continue
            if card.scope not in ("both", role):
                available = "、".join(c.name for c in self.list_for(role)) or "（暂无技能）"
                raise ValueError(
                    f"技能 {name!r} 仅对 {card.scope} 可见，当前角色是 {role}；当前可用：{available}"
                )
            return body
        available = "、".join(card.name for card in self.list_for(role)) or "（暂无技能）"
        raise ValueError(f"没有该技能: {name!r}；当前可用：{available}")
