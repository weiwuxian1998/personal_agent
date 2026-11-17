"""CUA 动作状态机与 `act` 批次语义（§8.9.4）。

- 12 个原语仍可单独调用；`act` 只是把它们**按顺序**串起来，一轮一个 `tool_call`；
- `act` 内部首错即停，未执行的动作标 `skipped`，绝不"接着试"；
- 每批**末尾只截一张图**（图不在 tool_result 里，由 `vision.py` 注入链路追加）；
- 步数预算按**物理动作**计，不按消息数（否则可以靠打包绕过硬上限）。

所有动作在同一把锁里执行：模型偶尔一轮返回多个 `tool_call` 时，ToolNode 会并发
派发（§8.9.2），锁保证输入序列不会交错；顺序本身仍由 `act` 保证。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from contracts import tool_error
from agents.vision import INJECT_MAX_IMAGES
from cua import input as cua_input
from cua import screen, windows

# 截图窗口的硬上限：注入层单次最多还原这么多张，超了也没意义。
MAX_SCREENSHOT_WINDOW = INJECT_MAX_IMAGES
DEFAULT_SCREENSHOT_WINDOW = 3


def effective_window(value: Any) -> int:
    """把配置钳到 [1, MAX_SCREENSHOT_WINDOW]；非法值退回默认 3。

    1 = 只有当前一张（退回旧行为）；调大看到更长的前后对比。
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_SCREENSHOT_WINDOW
    return max(1, min(n, MAX_SCREENSHOT_WINDOW))


@dataclass(frozen=True, slots=True)
class CuaLimits:
    max_batch: int = 3
    max_actions: int = 100
    wait_max_ms: int = 5000
    # 截图保留窗口：磁盘上只留最近 N 张（含当前），供模型前后对比。
    # 与 `agents/vision.py` 的注入上限（6）解耦：这里只留 3，注入天然不超过 3。
    retention: int = 3


OBSERVATION_ACTIONS = frozenset({"screenshot", "zoom", "list_windows", "session_state"})
EXECUTABLE_ACTIONS = frozenset(
    {"click", "mouse_move", "drag", "type", "key", "scroll", "wait", "focus_window"}
)
BUTTONS = frozenset({"left", "right", "middle"})
DIRECTIONS = frozenset({"up", "down", "left", "right"})


class CuaError(Exception):
    """动作失败；`code` 稳定，直接进工具返回的 `error_class`。"""

    def __init__(self, code: str, message: str, *, hint: str = "", **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.extra = extra

    def as_dict(self) -> dict:
        return tool_error(self.code, self.message, hint=self.hint, **self.extra)


class CuaSession:
    """一个任务一个实例；截图按序号落盘，只保留最近 `retention`（默认 3）张。

    人操作电脑有视觉记忆，能前后对比；这里给模型同样的能力：
    ｜shot-0010.jpg｜shot-0011.jpg｜shot-0012.jpg｜ ← 一次请求注入这 3 张（旧→新）
    写入第 13 张时删掉 0010，窗口滚成 0011/0012/0013。

    **内容判重**：新一帧的 JPEG 与上一帧字节完全相同（`sha256` 一致）时，不落新文件、
    也不占窗口位，直接复用上一帧的路径并在结果里标 `unchanged: true`——画面没变时
    既不会把同一张图塞进上下文，也不会让"当前状态"从模型视野里消失。窗口因此始终是
    "最近 N 个**不同**画面"，而不是"最近 N 次截图"。

    文件按序号命名，`agents/vision.py` 的按路径去重不会把同一张图重复注入；
    删除窗口外的文件后，历史里那些旧引用会加载失败，被注入层跳过——
    所以"一次请求最多 N 张"是由磁盘窗口保证的，不依赖模型配合。
    """

    def __init__(self, *, shots_dir: Path, limits: CuaLimits | None = None) -> None:
        self._lock = threading.RLock()
        self._limits = limits or CuaLimits()
        self._shots_dir = Path(shots_dir)
        self._seq = 0
        self._actions_used = 0
        self._last_image: dict | None = None
        # 上一张**不同画面**的整屏截图（判重基准）；zoom 不参与，避免和整屏混比。
        self._last_view: dict | None = None
        self._last_sha: str | None = None
        self._dup_streak = 0
        self._geom = screen.geometry(refresh=True)

    # ------------------------------------------------------------------ 元信息
    @property
    def limits(self) -> CuaLimits:
        return self._limits

    @property
    def actions_used(self) -> int:
        return self._actions_used

    @property
    def last_image(self) -> dict | None:
        return self._last_image

    def info(self) -> dict:
        return self._geom.as_dict()

    # ------------------------------------------------------------------ 内部
    def _consume(self, n: int = 1) -> None:
        if self._actions_used + n > self._limits.max_actions:
            raise CuaError(
                "step_budget_exceeded",
                f"本任务动作预算已用完（{self._actions_used}/{self._limits.max_actions}）",
                hint="停止操作，把当前进展和卡点写进结论交给 router",
            )
        self._actions_used += n

    def _guard_session(self) -> None:
        state = windows.session_state()
        if state.get("locked"):
            raise CuaError(
                "session_locked",
                "当前处于锁屏/UAC 安全桌面，无法安全注入输入",
                hint="不要重试；把情况写进结论，等用户解锁后重新派发",
                state={
                    "connect_state": state.get("connect_state"),
                    "input_desktop_ok": state.get("input_desktop_ok"),
                },
            )

    def _point(self, x: Any, y: Any) -> tuple[int, int]:
        try:
            fx, fy = float(x), float(y)
        except (TypeError, ValueError):
            raise CuaError(
                "bad_coordinates", f"坐标必须是数字: x={x!r}, y={y!r}",
                hint="坐标用最近一张截图的像素坐标",
            ) from None
        max_x, max_y = self._geom.model_w - 1, self._geom.model_h - 1
        # 允许 ±2 px 归一化取整误差，超出即报错（§8.3：不静默 clamp）
        if not (-2 <= fx <= max_x + 2 and -2 <= fy <= max_y + 2):
            raise CuaError(
                "coordinate_out_of_bounds",
                f"坐标 ({fx:g},{fy:g}) 超出截图范围 0..{max_x} × 0..{max_y}",
                hint="坐标必须来自最近一张截图（按图上的像素位置给）",
            )
        fx = min(max(fx, 0), max_x)
        fy = min(max(fy, 0), max_y)
        return self._geom.to_physical(fx, fy)

    def _next_path(self) -> Path:
        self._seq += 1
        return self._shots_dir / f"shot-{self._seq:06d}.jpg"

    def _prune_shots(self) -> None:
        """只留最近 `retention` 张；删失败不阻塞本轮（下次写入会再试）。"""
        keep = max(1, int(self._limits.retention))
        files = sorted(self._shots_dir.glob("shot-*.jpg"))
        for stale in files[:-keep]:
            try:
                stale.unlink()
            except OSError:
                pass

    def _capture_view(self) -> dict:
        """整屏截图；与上一张**不同画面**字节相同则丢弃新文件、复用上一张。

        判重依据是内容（`screen` 落盘时算的 JPEG sha256），与文件名/时间无关。
        复用的路径仍在窗口内，历史引用不会因此失效；窗口也不会被重复帧挤占。
        """
        path = self._next_path()
        info = screen.capture_view(path)
        sha = info.get("sha256")
        if self._last_view is not None and sha and sha == self._last_sha:
            try:
                path.unlink()  # 同一内容不留第二份，窗口位让给真正的变化
            except OSError:
                pass
            self._dup_streak += 1
            reused = {
                **self._last_view,
                "unchanged": True,
                "duplicate_frames": self._dup_streak,
            }
            self._last_image = reused
            return reused
        self._last_sha = sha
        self._dup_streak = 0
        self._last_view = info
        self._last_image = info
        self._prune_shots()
        return info

    @staticmethod
    def _image_payload(info: dict, **extra: Any) -> dict:
        payload = {
            "ok": True,
            "image_ref": info["path"],
            "width": info["width"],
            "height": info["height"],
            "origin": info.get("origin", [0, 0]),
            "scale": info.get("scale"),
            "mean_luma": info.get("mean_luma"),
            "bytes": info.get("bytes"),
            # 画面与上一帧完全一致时为 true：图仍是"当前画面"，只是没有新的变化。
            "unchanged": bool(info.get("unchanged")),
            "duplicate_frames": int(info.get("duplicate_frames") or 0),
        }
        payload.update(extra)
        return payload

    # ------------------------------------------------------------------ 观测
    def screenshot(self) -> dict:
        with self._lock:
            info = self._capture_view()
            return self._image_payload(info, model_space=self._geom.as_dict())

    def zoom(self, x0: Any, y0: Any, x1: Any, y1: Any) -> dict:
        try:
            box = tuple(float(v) for v in (x0, y0, x1, y1))
        except (TypeError, ValueError):
            raise CuaError("bad_region", "region 必须是 4 个数字") from None
        a, b, c, d = box
        if not (0 <= a < c <= self._geom.model_w and 0 <= b < d <= self._geom.model_h):
            raise CuaError(
                "bad_region",
                f"region 必须是 0≤x0<x1≤{self._geom.model_w}、0≤y0<y1≤{self._geom.model_h}",
                hint="region 用整张截图的像素坐标，不是裁剪图的局部坐标",
            )
        with self._lock:
            info = screen.capture_region(
                (int(a), int(b), int(c), int(d)), self._next_path()
            )
            self._last_image = info
            # 局部图与整屏图不可比：下一次整屏截图不做判重，正常落盘。
            self._last_view = None
            self._last_sha = None
            self._dup_streak = 0
            self._prune_shots()
            return self._image_payload(
                info,
                region=[int(a), int(b), int(c), int(d)],
                note="坐标仍按整张截图的像素空间表达",
            )

    # ------------------------------------------------------------------ 原语
    def click(self, x: Any, y: Any, *, button: str = "left", count: int = 1) -> dict:
        button = str(button or "left").lower()
        if button not in BUTTONS:
            raise CuaError("bad_button", f"button 只能是 left/right/middle，收到 {button!r}")
        try:
            count = int(count)
        except (TypeError, ValueError):
            raise CuaError("bad_count", "count 必须是 1/2/3") from None
        if count not in (1, 2, 3):
            raise CuaError("bad_count", "count 只能是 1/2/3")
        with self._lock:
            self._consume()
            self._guard_session()
            px, py = self._point(x, y)
            res = cua_input.click_at(px, py, button=button, count=count)
            return {
                "ok": True,
                "at": [x, y],
                "physical": [px, py],
                "button": button,
                "count": count,
                "cursor_delta": res["moved"]["delta"],
            }

    def mouse_move(self, x: Any, y: Any) -> dict:
        with self._lock:
            self._consume()
            self._guard_session()
            px, py = self._point(x, y)
            res = cua_input.move_to(px, py)
            return {"ok": True, "at": [x, y], "physical": [px, py], "cursor_delta": res["delta"]}

    def drag(
        self,
        from_xy: Any,
        to_xy: Any,
        *,
        button: str = "left",
        duration_ms: int = 300,
    ) -> dict:
        # 参数名用 from/to 更贴合模型习惯，Python 侧保留关键字安全的名字。
        button = str(button or "left").lower()
        if button != "left":
            raise CuaError("bad_button", "drag 只支持 left（拖拽语义）")
        try:
            (fx, fy), (tx, ty) = from_xy, to_xy
        except (TypeError, ValueError):
            raise CuaError("bad_drag", "from / to 必须是 [x, y]") from None
        try:
            duration_ms = max(50, min(2000, int(duration_ms)))
        except (TypeError, ValueError):
            raise CuaError("bad_duration", "duration_ms 必须是整数") from None
        with self._lock:
            self._consume()
            self._guard_session()
            p_from = self._point(fx, fy)
            p_to = self._point(tx, ty)
            cua_input.drag(p_from, p_to, duration_ms=duration_ms)
            return {
                "ok": True,
                "from": [fx, fy],
                "to": [tx, ty],
                "physical": {"from": list(p_from), "to": list(p_to)},
            }

    def type(self, text: Any) -> dict:
        if not isinstance(text, str) or not text:
            raise CuaError("bad_text", "text 必须是非空字符串")
        if len(text) > 4000:
            raise CuaError("text_too_long", "单次 type 最多 4000 字符")
        with self._lock:
            self._consume()
            self._guard_session()
            res = cua_input.type_text(text)
            return {"ok": True, "chars": res["chars"], "accepted": res["accepted"]}

    def key(self, keys: Any) -> dict:
        if not isinstance(keys, str) or not keys.strip():
            raise CuaError("bad_keys", 'keys 不能为空，例如 "enter" 或 "ctrl+s"')
        with self._lock:
            self._consume()
            self._guard_session()
            try:
                res = cua_input.press_chord(keys)
            except ValueError as exc:
                raise CuaError("bad_keys", str(exc)) from None
            if not res.get("ok"):
                # IME 拦截是环境事实，不是参数错误：连同 hint 一起回给模型。
                return {
                    "ok": False,
                    "error_class": res.get("error", "key_failed"),
                    "message": res.get("detail", "按键未生效"),
                    "hint": res.get("hint", ""),
                    "keys": keys,
                }
            return {"ok": True, "keys": keys}

    def scroll(self, x: Any, y: Any, *, direction: str, amount: int = 3) -> dict:
        direction = str(direction or "").lower()
        if direction not in DIRECTIONS:
            raise CuaError("bad_direction", "direction 只能是 up/down/left/right")
        try:
            amount = int(amount)
        except (TypeError, ValueError):
            raise CuaError("bad_amount", "amount 必须是整数") from None
        with self._lock:
            self._consume()
            self._guard_session()
            px, py = self._point(x, y)
            res = cua_input.scroll_at(px, py, direction=direction, amount=amount)
            return {
                "ok": True,
                "notches": res["notches"],
                "direction": direction,
                "at": [x, y],
            }

    def wait(self, ms: Any) -> dict:
        try:
            value = int(ms)
        except (TypeError, ValueError):
            raise CuaError("bad_duration", "ms 必须是整数") from None
        value = max(0, min(self._limits.wait_max_ms, value))
        with self._lock:
            self._consume()
            time.sleep(value / 1000)
            return {"ok": True, "ms": value}

    def focus_window(self, hwnd: Any) -> dict:
        try:
            h = int(hwnd)
        except (TypeError, ValueError):
            raise CuaError("bad_hwnd", "hwnd 必须是整数（取自 list_windows）") from None
        with self._lock:
            self._consume()
            self._guard_session()
            res = windows.focus_window(h)
            if not res.get("ok"):
                raise CuaError(
                    res.get("error", "focus_failed"),
                    res.get("message", "聚焦失败"),
                    hint=res.get("hint", ""),
                )
            return res

    # ------------------------------------------------------------------ 分发
    def dispatch(self, name: str, args: Any) -> tuple[bool, dict]:
        """执行单个动作；返回 (ok, payload)。任何异常都转成结构化失败。"""
        args = args if isinstance(args, dict) else {}
        handler: Callable[[dict], dict] | None = {
            "click": lambda a: self.click(
                a.get("x"), a.get("y"),
                button=a.get("button", "left"), count=a.get("count", 1),
            ),
            "mouse_move": lambda a: self.mouse_move(a.get("x"), a.get("y")),
            "drag": lambda a: self.drag(
                a.get("from") or [a.get("x0"), a.get("y0")],
                a.get("to") or [a.get("x1"), a.get("y1")],
                duration_ms=a.get("duration_ms", 300),
            ),
            "type": lambda a: self.type(a.get("text")),
            "key": lambda a: self.key(a.get("keys")),
            "scroll": lambda a: self.scroll(
                a.get("x"), a.get("y"),
                direction=a.get("direction", ""), amount=a.get("amount", 3),
            ),
            "wait": lambda a: self.wait(a.get("ms", 500)),
            "focus_window": lambda a: self.focus_window(a.get("hwnd")),
            "list_windows": lambda _a: windows.list_windows(),
            "session_state": lambda _a: self._session_payload(),
            "screenshot": lambda _a: self.screenshot(),
            "zoom": lambda a: self.zoom(
                a.get("x0"), a.get("y0"), a.get("x1"), a.get("y1")
            ),
        }.get(name)
        if handler is None:
            return False, tool_error(
                "unknown_action", f"未知动作: {name}", hint="可用动作见 act 工具的说明"
            )
        try:
            payload = handler(args)
            return bool(payload.get("ok", True)), payload
        except CuaError as exc:
            return False, exc.as_dict()
        except Exception as exc:  # noqa: BLE001 - 注入层异常必须变成可读结果
            return False, tool_error("action_failed", f"{type(exc).__name__}: {exc}")

    def _session_payload(self) -> dict:
        state = windows.session_state()
        state["actions_used"] = self._actions_used
        state["max_actions"] = self._limits.max_actions
        state["screenshot_window"] = self._limits.retention
        state["screen"] = self._geom.as_dict()
        return state

    # ------------------------------------------------------------------ 批次
    def act(self, actions: Any, stop_on_error: bool = True) -> dict:
        if not isinstance(actions, list) or not actions:
            raise CuaError("bad_batch", "actions 必须是非空数组")
        if len(actions) > self._limits.max_batch:
            raise CuaError(
                "batch_too_long",
                f"一批最多 {self._limits.max_batch} 个动作，收到 {len(actions)}",
                hint="拆成多批；批次末尾会返回一张截图，看完再决定下一步",
            )

        results: list[dict] = []
        stopped_at: int | None = None
        with self._lock:
            for i, item in enumerate(actions):
                raw = item if isinstance(item, dict) else {}
                name = str(raw.get("name") or "").strip()
                args = raw.get("args") if isinstance(raw.get("args"), dict) else {}

                if stopped_at is not None:
                    results.append(
                        {"i": i, "name": name or "?", "ok": False,
                         "skipped": True, "error": "not_executed"}
                    )
                    continue

                if name in OBSERVATION_ACTIONS or name == "act":
                    results.append(
                        {
                            "i": i, "name": name, "ok": False,
                            "error_class": "action_not_allowed",
                            "message": "观测类动作与 act 不能放进批次，留在批外单独调用",
                        }
                    )
                    stopped_at = i
                    continue
                if name not in EXECUTABLE_ACTIONS:
                    results.append(
                        {
                            "i": i, "name": name or "?", "ok": False,
                            "error_class": "unknown_action",
                            "message": f"未知动作: {name}",
                        }
                    )
                    stopped_at = i
                    continue

                ok, payload = self.dispatch(name, args)
                row = {"i": i, "name": name, "ok": ok}
                row.update({k: v for k, v in payload.items() if k != "ok"})
                results.append(row)
                if not ok and stop_on_error:
                    stopped_at = i

            # 批末固定一张图：跨轮观测不缺失（§8.9.5）。
            image: dict | None
            try:
                image = self._capture_view()
            except Exception as exc:  # noqa: BLE001 - 截图失败不掩盖动作结果
                image = None
                image_error = f"{type(exc).__name__}: {exc}"
            else:
                image_error = ""

        payload: dict[str, Any] = {
            # `ok` 表示"整批是否全部成功"，不是"截图是否成功"。
            "ok": stopped_at is None,
            "actions": results,
            "stopped_at": stopped_at,
            "actions_used": self._actions_used,
            "max_actions": self._limits.max_actions,
        }
        if image is not None:
            payload.update(
                {k: v for k, v in self._image_payload(image).items() if k != "ok"}
            )
            payload["model_space"] = self._geom.as_dict()
        else:
            payload["image_ref"] = ""
            payload["image_error"] = image_error
        return payload
