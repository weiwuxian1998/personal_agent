"""对模型暴露的 CUA 工具层：12 个原语 + 1 个批次入口 `act`（§8.0 / §8.2 / §8.9.4）。

通道约定（重要）：
- 工具只回**文本**，图片用 `[图片] <路径> | <mime> | <n> 字节` 引用；
- 引用必须放在**独立的文本块**里（`agents.vision.inject_image_blocks` 按整块文本匹配），
  由装配层在请求末尾追加一条带图 user 消息（网关禁止 tool 角色带图）；
- 截图按序号落盘、只留最近 `cua_screenshot_window` 张；与上一帧**内容相同**的
  连拍不落新文件（复用上一帧路径并标 `unchanged: true`），所以一次请求注入的就是
  窗口内这几个不同画面，既不重复、也不会超过注入层上限（配置越界会被钳制并告警）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

from agents.vision import format_image_reference
from config import Settings
from contracts import TaskSpec
from cua.session import (
    MAX_SCREENSHOT_WINDOW,
    CuaError,
    CuaLimits,
    CuaSession,
    effective_window,
)

logger = logging.getLogger("cua.tools")

_ACT_ACTIONS = """
可用动作与参数（坐标一律是最近一张截图的像素坐标）：
- click {x, y, button?: "left"|"right"|"middle", count?: 1|2|3}
- mouse_move {x, y}
- drag {from: [x, y], to: [x, y], duration_ms?: 50..2000}
- type {text}            （Unicode 键入，不受输入法影响）
- key {keys}             （如 "enter" / "ctrl+s" / "f5"；本机可能返回 ime_intercept）
- scroll {x, y, direction: "up"|"down"|"left"|"right", amount?: 1..10}
- wait {ms}              （上限 5000）
- focus_window {hwnd}    （hwnd 取自 list_windows）
""".strip()

_ACT_NOT_ALLOWED = (
    "screenshot / zoom / list_windows / session_state 不能放进批次："
    "观测留在批外，批次末尾会自动返回一张截图。"
)

_ACT_DESCRIPTION = f"""按顺序执行一批 GUI 动作（1–3 个），首错即停，批次末尾返回一张最新截图。

这是默认通道：一次调用只产生一条工具结果，天然串行；跨轮永远有一张图。
需要逐步观察时，就用只有一个动作的批次，或直接调 screenshot。

{_ACT_ACTIONS}

{_ACT_NOT_ALLOWED}

参数：
- actions: 动作数组，每项形如 {{"name": "click", "args": {{"x": 640, "y": 360}}}}。
- stop_on_error: 第一个动作失败后是否停止（默认 true；不要改成 false）。
"""


def _wrap(session: CuaSession, payload: dict) -> list[dict]:
    """把动作结果转成内容块：JSON 文本 + （可选）图片引用块。"""
    blocks: list[dict] = [
        {"type": "text", "text": json.dumps(payload, ensure_ascii=False)}
    ]
    info = session.last_image
    if payload.get("image_ref") and info and info.get("path") == payload["image_ref"]:
        blocks.append(
            {
                "type": "text",
                "text": format_image_reference(info["path"], info["mime"], info["bytes"]),
            }
        )
    return blocks


def _run(session: CuaSession, name: str, args: dict) -> list[dict]:
    _ok, payload = session.dispatch(name, args)
    return _wrap(session, payload)


def build_cua_tools(*, settings: Settings, task: TaskSpec) -> list[Any]:
    configured = int(settings.cua_screenshot_window)
    window = effective_window(configured)
    if window != configured:
        logger.warning(
            "cua_screenshot_window=%s 超出允许范围 [1, %s]，已钳制为 %s",
            configured,
            MAX_SCREENSHOT_WINDOW,
            window,
        )
    shots_dir = Path(settings.tool_outputs_dir) / "cua" / str(task.task_id) / "shots"
    session = CuaSession(
        shots_dir=shots_dir,
        limits=CuaLimits(
            max_batch=int(settings.cua_max_batch),
            max_actions=int(settings.cua_max_actions),
            wait_max_ms=int(settings.cua_wait_max_ms),
            retention=window,
        ),
    )

    @tool("screenshot")
    def screenshot_tool() -> list[dict]:
        """截取整屏（单屏），返回最新画面与坐标空间信息。

        返回里带 width/height/scale/origin：你之后给的所有坐标都必须是这张图的像素坐标。
        """
        return _run(session, "screenshot", {})

    @tool("zoom")
    def zoom_tool(x0: int, y0: int, x1: int, y1: int) -> list[dict]:
        """放大截取某个矩形区域，用于读小字/长文本。

        region 用**整张截图**的像素坐标（不是裁剪图的局部坐标）；返回的图是 1:1 裁剪，
        但你之后的坐标仍按整张截图的空间表达。
        """
        return _run(session, "zoom", {"x0": x0, "y0": y0, "x1": x1, "y1": y1})

    @tool("click")
    def click_tool(x: int, y: int, button: str = "left", count: int = 1) -> list[dict]:
        """在截图坐标 (x, y) 点击。button=left/right/middle，count=1/2/3（双击、三击）。"""
        return _run(session, "click", {"x": x, "y": y, "button": button, "count": count})

    @tool("mouse_move")
    def mouse_move_tool(x: int, y: int) -> list[dict]:
        """把鼠标移到 (x, y) 不点击；用于触发 hover 菜单/提示。"""
        return _run(session, "mouse_move", {"x": x, "y": y})

    @tool("drag")
    def drag_tool(x0: int, y0: int, x1: int, y1: int, duration_ms: int = 300) -> list[dict]:
        """按住左键从 (x0, y0) 拖到 (x1, y1)。用于拖选、拖放、拉滑杆。"""
        return _run(
            session,
            "drag",
            {"from": [x0, y0], "to": [x1, y1], "duration_ms": duration_ms},
        )

    @tool("type")
    def type_tool(text: str) -> list[dict]:
        """向当前焦点键入文本（Unicode 路径，中文/符号均可）。先点击目标输入框再调用。"""
        return _run(session, "type", {"text": text})

    @tool("key")
    def key_tool(keys: str) -> list[dict]:
        """按一次键或组合键，如 "enter"、"tab"、"ctrl+s"、"f5"。

        本机中文输入法打开时 VK 会被吞（返回 ime_intercept），此时改用鼠标点击菜单，
        不要反复重试同一个键。
        """
        return _run(session, "key", {"keys": keys})

    @tool("scroll")
    def scroll_tool(x: int, y: int, direction: str, amount: int = 3) -> list[dict]:
        """在 (x, y) 处滚动滚轮。direction=up/down/left/right，amount 是格数。"""
        return _run(
            session, "scroll", {"x": x, "y": y, "direction": direction, "amount": amount}
        )

    @tool("wait")
    def wait_tool(ms: int = 500) -> list[dict]:
        """等待指定毫秒（上限 5000），用于等加载/等动画。"""
        return _run(session, "wait", {"ms": ms})

    @tool("list_windows")
    def list_windows_tool() -> list[dict]:
        """列出可见的顶层窗口（hwnd / pid / 进程名 / 标题 / 类名 / 矩形 / 是否焦点）。

        纯视觉在桌面上先靠它确认"该操作哪个窗口"，再用 focus_window 切过去。
        """
        return _run(session, "list_windows", {})

    @tool("focus_window")
    def focus_window_tool(hwnd: int) -> list[dict]:
        """把指定窗口带到前台（必要时从最小化恢复）。hwnd 取自 list_windows。"""
        return _run(session, "focus_window", {"hwnd": hwnd})

    @tool("session_state")
    def session_state_tool() -> list[dict]:
        """查询会话是否可以安全操作：是否锁屏/UAC 安全桌面、动作预算、屏幕与 DPI 信息。"""
        return _run(session, "session_state", {})

    @tool("act", description=_ACT_DESCRIPTION)
    def act_tool(actions: list[dict], stop_on_error: bool = True) -> list[dict]:
        """按顺序执行一批 GUI 动作（描述见模块常量 _ACT_DESCRIPTION）。"""
        try:
            payload = session.act(actions, stop_on_error=bool(stop_on_error))
        except CuaError as exc:
            payload = exc.as_dict()
        return _wrap(session, payload)

    return [
        screenshot_tool,
        zoom_tool,
        click_tool,
        mouse_move_tool,
        drag_tool,
        type_tool,
        key_tool,
        scroll_tool,
        wait_tool,
        list_windows_tool,
        focus_window_tool,
        session_state_tool,
        act_tool,
    ]
