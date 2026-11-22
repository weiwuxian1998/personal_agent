"""UIA 覆盖面探针（纯 ctypes，无第三方依赖）。

目的：在决定"要不要把无障碍树接进 cua 循环"之前，先量清楚
  1) 每个窗口的 UIA 节点数（token 预算的分母）；
  2) 有多少节点带名字 / 带包围盒（可寻址的比例）；
  3) 有多少节点是可交互的（Invoke/Value/Toggle/SelectionItem/ExpandCollapse）；
  4) 遍历一次要多久（会不会拖慢一轮 act）。

用法：
  python scripts/probe_uia.py            # 前台窗口
  python scripts/probe_uia.py --all      # 所有可见顶层窗口
  python scripts/probe_uia.py --hwnd 1234 --rows 40
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import sys
import time
from dataclasses import dataclass

# --------------------------------------------------------------------------- COM 基础
ole32 = ctypes.WinDLL("ole32")
oleaut32 = ctypes.WinDLL("oleaut32")
user32 = ctypes.WinDLL("user32", use_last_error=True)

CLSCTX_INPROC_SERVER = 1
COINIT_MULTITHREADED = 0x0

CLSID_CUIAutomation = "{FF48DBA4-60EF-4201-AA87-54103EEF594E}"
IID_IUIAutomation = "{30CBE57D-D9D0-452A-AB13-7AC5AC4825EE}"

# IUIAutomation vtable（IUnknown 之后从 3 起）
UIA_ELEMENT_FROM_HANDLE = 6
UIA_GET_CONTROL_VIEW_WALKER = 14
UIA_GET_CONTENT_VIEW_WALKER = 15

# IUIAutomationElement vtable
ELEM_GET_CURRENT_PROPERTY = 10
ELEM_CURRENT_PATTERN = 14

# IUIAutomationTreeWalker vtable
TW_FIRST_CHILD = 4
TW_NEXT_SIBLING = 6

# PropertyId
P_CONTROL_TYPE = 30003
P_NAME = 30005
P_BOUNDING_RECT = 30001
P_IS_ENABLED = 30010
P_IS_OFFSCREEN = 30022
P_AUTOMATION_ID = 30011
P_CLASS_NAME = 30012
P_VALUE = 30045
P_IS_PASSWORD = 30019
P_IS_KEYBOARD_FOCUSABLE = 30009

# XXXIsYYYPatternAvailable
P_HAS_INVOKE = 30031
P_HAS_TOGGLE = 30035
P_HAS_SELECTION_ITEM = 30037
P_HAS_EXPAND_COLLAPSE = 30044
P_HAS_VALUE = 30043
P_HAS_TEXT = 30040

VT_I4, VT_R8, VT_BSTR, VT_BOOL, VT_UNKNOWN, VT_ARRAY, VT_EMPTY = 3, 5, 8, 11, 13, 0x2000, 0

CONTROL_TYPE_NAMES = {
    50000: "Button", 50001: "Calendar", 50002: "CheckBox", 50003: "ComboBox",
    50004: "Edit", 50005: "Hyperlink", 50006: "Image", 50007: "ListItem",
    50008: "List", 50009: "Menu", 50010: "MenuBar", 50011: "MenuItem",
    50012: "ProgressBar", 50013: "RadioButton", 50014: "ScrollBar",
    50015: "Slider", 50016: "Spinner", 50017: "StatusBar", 50018: "Tab",
    50019: "TabItem", 50020: "Text", 50021: "ToolBar", 50022: "ToolTip",
    50023: "Tree", 50024: "TreeItem", 50025: "Custom", 50026: "Group",
    50027: "Thumb", 50028: "DataGrid", 50029: "DataItem", 50030: "Document",
    50031: "SplitButton", 50032: "Window", 50033: "Pane", 50034: "Header",
    50035: "HeaderItem", 50036: "Table", 50037: "TitleBar", 50038: "Separator",
    50039: "SemanticZoom", 50040: "AppBar",
}

ACTIONABLE = ("Invoke", "Toggle", "SelectionItem", "ExpandCollapse", "Value", "Text")


@dataclass
class Variant(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [
            ("llVal", ctypes.c_longlong),
            ("lVal", ctypes.c_long),
            ("dblVal", ctypes.c_double),
            ("boolVal", ctypes.c_short),
            ("bstrVal", ctypes.c_void_p),
            ("punkVal", ctypes.c_void_p),
            ("pArray", ctypes.c_void_p),
            ("rgd", ctypes.c_double * 4),
        ]

    _fields_ = [
        ("vt", ctypes.c_ushort),
        ("r1", ctypes.c_ushort),
        ("r2", ctypes.c_ushort),
        ("r3", ctypes.c_ushort),
        ("u", _U),
    ]


def _vtable(ptr: int) -> ctypes.Array:
    return ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]


def _call(ptr: int, index: int, restype, argtypes, *args):
    fn = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(_vtable(ptr)[index])
    return fn(ptr, *args)


def _guid(text: str):
    class GUID(ctypes.Structure):
        _fields_ = [
            ("d1", ctypes.c_ulong), ("d2", ctypes.c_ushort),
            ("d3", ctypes.c_ushort), ("d4", ctypes.c_ubyte * 8),
        ]

    g = GUID()
    ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(g))
    return g


def _com_ready() -> bool:
    hr = ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
    return hr in (0, 1)  # S_OK / S_FALSE


def create_uia() -> int:
    ptr = ctypes.c_void_p()
    hr = ole32.CoCreateInstance(
        ctypes.byref(_guid(CLSID_CUIAutomation)), None, CLSCTX_INPROC_SERVER,
        ctypes.byref(_guid(IID_IUIAutomation)), ctypes.byref(ptr),
    )
    if hr != 0 or not ptr.value:
        raise OSError(f"CoCreateInstance(IUIAutomation) 失败 hr=0x{hr & 0xFFFFFFFF:08X}")
    return ptr.value


def _prop(elem: int, prop_id: int) -> Variant:
    v = Variant()
    _call(elem, ELEM_GET_CURRENT_PROPERTY, ctypes.c_long,
          [ctypes.c_int, ctypes.POINTER(Variant)], prop_id, ctypes.byref(v))
    return v


def prop_int(elem: int, prop_id: int) -> int:
    v = _prop(elem, prop_id)
    if v.vt == VT_I4:
        return int(v.u.lVal)
    return -1


def prop_bool(elem: int, prop_id: int) -> bool:
    v = _prop(elem, prop_id)
    return bool(v.vt == VT_BOOL and v.u.boolVal)


def prop_str(elem: int, prop_id: int) -> str:
    v = _prop(elem, prop_id)
    if v.vt != VT_BSTR or not v.u.bstrVal:
        return ""
    text = ctypes.wstring_at(v.u.bstrVal)
    oleaut32.SysFreeString(ctypes.c_void_p(v.u.bstrVal))
    return text


def prop_rect(elem: int) -> tuple[float, float, float, float] | None:
    """UIA_BoundingRectangle 返回 double[4] = (left, top, width, height)，**不是 RECT**。

    实测踩过：按 RECT 解析会得到 right<left 的"合法"数字（例如 Text 节点
    [104,7,28,16] 其实是 x=104,y=7,w=28,h=16），不校验的话坐标会静默错位。
    """
    v = _prop(elem, P_BOUNDING_RECT)
    if v.vt != (VT_ARRAY | VT_R8) or not v.u.pArray:
        return None
    sa = v.u.pArray
    vals = (ctypes.c_double * 4)()
    for i in range(4):
        idx = ctypes.c_long(i)
        oleaut32.SafeArrayGetElement(
            ctypes.c_void_p(sa), ctypes.byref(idx), ctypes.byref(vals, i * 8)
        )
    oleaut32.SafeArrayDestroy(ctypes.c_void_p(sa))
    left, top, width, height = vals
    if width <= 0 or height <= 0:
        return None
    return (float(left), float(top), float(width), float(height))


def elem_from_handle(uia: int, hwnd: int) -> int | None:
    ptr = ctypes.c_void_p()
    hr = _call(uia, UIA_ELEMENT_FROM_HANDLE, ctypes.c_long,
               [wt.HWND, ctypes.POINTER(ctypes.c_void_p)], wt.HWND(hwnd), ctypes.byref(ptr))
    if hr != 0 or not ptr.value:
        return None
    return ptr.value


def view_walker(uia: int, index: int) -> int:
    ptr = ctypes.c_void_p()
    _call(uia, index, ctypes.c_long, [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(ptr))
    return ptr.value or 0


def child(walker: int, elem: int) -> int:
    ptr = ctypes.c_void_p()
    _call(walker, TW_FIRST_CHILD, ctypes.c_long,
          [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)], ctypes.c_void_p(elem), ctypes.byref(ptr))
    return ptr.value or 0


def sibling(walker: int, elem: int) -> int:
    ptr = ctypes.c_void_p()
    _call(walker, TW_NEXT_SIBLING, ctypes.c_long,
          [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)], ctypes.c_void_p(elem), ctypes.byref(ptr))
    return ptr.value or 0


# --------------------------------------------------------------------------- 遍历
@dataclass
class Row:
    depth: int
    role: str
    name: str
    rect: tuple[float, float, float, float] | None
    enabled: bool
    offscreen: bool
    patterns: str
    value: str
    auto_id: str
    klass: str


def walk(uia: int, hwnd: int, *, max_nodes: int, max_depth: int,
         content_view: bool) -> tuple[list[Row], float]:
    elem = elem_from_handle(uia, hwnd)
    if not elem:
        return [], 0.0
    index = UIA_GET_CONTENT_VIEW_WALKER if content_view else UIA_GET_CONTROL_VIEW_WALKER
    walker = view_walker(uia, index)
    if not walker:
        return [], 0.0

    out: list[Row] = []
    started = time.perf_counter()
    stack = [(elem, 0)]
    while stack and len(out) < max_nodes:
        node, depth = stack.pop()
        ct = CONTROL_TYPE_NAMES.get(prop_int(node, P_CONTROL_TYPE), "?")
        patterns = []
        for pid, label in (
            (P_HAS_INVOKE, "Invoke"), (P_HAS_TOGGLE, "Toggle"),
            (P_HAS_SELECTION_ITEM, "SelectionItem"), (P_HAS_EXPAND_COLLAPSE, "Expand"),
            (P_HAS_VALUE, "Value"), (P_HAS_TEXT, "Text"),
        ):
            if prop_bool(node, pid):
                patterns.append(label)
        out.append(
            Row(
                depth=depth,
                role=ct,
                name=prop_str(node, P_NAME)[:80],
                rect=prop_rect(node),
                enabled=prop_bool(node, P_IS_ENABLED),
                offscreen=prop_bool(node, P_IS_OFFSCREEN),
                patterns=",".join(patterns),
                value=prop_str(node, P_VALUE)[:60],
                auto_id=prop_str(node, P_AUTOMATION_ID)[:40],
                klass=prop_str(node, P_CLASS_NAME)[:40],
            )
        )
        if depth >= max_depth:
            continue
        kids = []
        kid = child(walker, node)
        while kid:
            kids.append(kid)
            kid = sibling(walker, kid)
        # 逆序入栈，出栈即文档序
        for kid in reversed(kids):
            stack.append((kid, depth + 1))
    elapsed = (time.perf_counter() - started) * 1000
    return out, elapsed


# --------------------------------------------------------------------------- 报告
_ENUM_PROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def visible_windows(limit: int = 40) -> list[tuple[int, str, str]]:
    user32.GetWindowTextLengthW.argtypes = [wt.HWND]
    rows: list[tuple[int, str, str]] = []

    def _cb(hwnd, _lp):
        h = int(hwnd)
        if not user32.IsWindowVisible(h):
            return True
        n = user32.GetWindowTextLengthW(wt.HWND(h))
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(wt.HWND(h), buf, n + 1)
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(wt.HWND(h), cls, 256)
        rows.append((h, buf.value[:50], cls.value))
        return True

    user32.EnumWindows(_ENUM_PROC(_cb), 0)
    return rows[:limit]


def summarize(label: str, rows: list[Row], elapsed: float, max_nodes: int) -> None:
    if not rows:
        print(f"  [x] {label}: UIA 拿不到元素（0 节点）")
        return
    named = sum(1 for r in rows if r.name)
    with_rect = sum(1 for r in rows if r.rect)
    actionable = sum(1 for r in rows if r.patterns)
    truncated = " (截断)" if len(rows) >= max_nodes else ""
    print(f"  [v] {label}")
    print(f"      节点 {len(rows)}{truncated} | 遍历 {elapsed:.0f} ms | "
          f"有名字 {named} ({named * 100 // len(rows)}%) | "
          f"有包围盒 {with_rect} ({with_rect * 100 // len(rows)}%) | "
          f"可交互 {actionable} ({actionable * 100 // len(rows)}%)")
    offs = sum(1 for r in rows if r.offscreen)
    no_rect_visible = sum(
        1 for r in rows
        if not r.offscreen and not r.rect
    )
    print(f"      offscreen {offs} | 可见但无包围盒 {no_rect_visible}（寻址盲区）")
    hist: dict[str, int] = {}
    for r in rows:
        hist[r.role] = hist.get(r.role, 0) + 1
    top = sorted(hist.items(), key=lambda kv: -kv[1])[:8]
    print("      类型：" + "  ".join(f"{k}={v}" for k, v in top))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hwnd", type=int, default=0)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--rows", type=int, default=0, help="打印前 N 个元素明细")
    ap.add_argument("--max-nodes", type=int, default=600)
    ap.add_argument("--max-depth", type=int, default=10)
    ap.add_argument("--content", action="store_true", help="用 ContentView（默认 ControlView）")
    args = ap.parse_args(argv)

    _com_ready()
    uia = create_uia()
    print(f"IUIAutomation 就绪  视图={'Content' if args.content else 'Control'}  "
          f"上限={args.max_nodes} 节点/{args.max_depth} 层\n")

    targets: list[tuple[int, str]] = []
    if args.hwnd:
        targets = [(args.hwnd, f"hwnd={args.hwnd}")]
    elif args.all:
        targets = [(h, f"{title}  [{cls}]") for h, title, cls in visible_windows()]
    else:
        h = int(user32.GetForegroundWindow() or 0)
        targets = [(h, f"前台窗口 hwnd={h}")]

    for hwnd, label in targets:
        rows, elapsed = walk(
            uia, hwnd, max_nodes=args.max_nodes, max_depth=args.max_depth,
            content_view=args.content,
        )
        summarize(label, rows, elapsed, args.max_nodes)
        if args.rows:
            for r in rows[: args.rows]:
                rect = "-" if not r.rect else ",".join(f"{v:.0f}" for v in r.rect)
                print(f"      {'  ' * r.depth}{r.role:<14} name={r.name!r} "
                      f"rect=[{rect}] pat={r.patterns}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
