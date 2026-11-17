"""纯视觉 Computer Use（§8 定稿）。

只做**单屏**、**纯视觉**：线上循环里不出现 ref / DOM 快照 / a11y 树。
`elementFromPoint` 一类只允许用于离线诊断与基准的 ground truth，不在本包里。

模块划分：
- `dpi`：进程启动即声明 per-monitor V2（§8.3 硬要求）；
- `screen`：整屏截图、缩放、坐标空间换算（固定 1.5×，由长边上限算出）；
- `input`：SendInput 鼠标/键盘/滚轮/拖拽（桌面实测通过的路径）；
- `windows`：窗口枚举、聚焦、会话状态；
- `session`：动作状态机与 `act` 批次语义；
- `tools`：12 个原语 + 1 个批次入口（对模型暴露的层）；
- `browser`：浏览器内核启动（单点配置，不是模型工具）。

注意：本包会加载 user32/gdi32，只在 `task_kind="cua"` 时才导入。
"""

from __future__ import annotations
