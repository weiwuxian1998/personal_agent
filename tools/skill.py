"""Skill：按需读取技能说明（契约 §7.5 B1）。

- `Skill()` 无参 → 返回当前角色可见的技能卡片列表（name + description）；
- `Skill(name="xxx")` → 返回该技能的正文（Markdown 字符串）；
- 技能不存在 / 越 scope → 结构化错误，不抛异常。

技能文件由 `skills/registry.py` 扫描，加技能只需要加文件。
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool, tool

from contracts import tool_error
from skills.registry import SkillRegistry

_SKILL_HINT = "先用 Skill() 查看可用技能；不要凭猜测调用技能名。"


def build_skill_tools(*, registry: SkillRegistry, role: str) -> list[BaseTool]:
    """构造 `[Skill]`；`role` ∈ router / worker。"""

    @tool
    def Skill(name: str | None = None) -> Any:
        """查看或加载技能说明。

        Args:
            name: 技能名；省略时返回可用技能列表（名称 + 一句话说明）。
        """
        if not name:
            return [
                {"name": card.name, "description": card.description}
                for card in registry.list_for(role)
            ]
        try:
            return registry.load(name, role)
        except ValueError as exc:
            return tool_error("skill_unavailable", str(exc), hint=_SKILL_HINT)

    return [Skill]
