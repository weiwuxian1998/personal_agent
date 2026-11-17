"""技能包：按角色（router / worker）挂载的 Markdown 技能。

目录约定（契约 §7.5 B1）：
    skills/shared/**        scope: both 的技能
    skills/router-only/**   scope: router
    skills/worker-only/**   scope: worker

技能文件格式（frontmatter 必含 name / description / scope）：

    ---
    name: pdf-report
    description: 把调研结果整理成 PDF 报告
    scope: both
    ---
    ## 用法
    ...
"""

from skills.registry import SkillCard, SkillRegistry, parse_skill

__all__ = ["SkillCard", "SkillRegistry", "parse_skill"]
