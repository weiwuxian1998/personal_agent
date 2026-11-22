"""UIA 实验台：把「逐属性遍历」和「CacheRequest 批量取」放在同一棵树上对跑。

只读实验（默认）。`--setvalue` 会额外开一个记事本做 ValuePattern 写入实验（有副作用，需显式开启）。

三个实验：
  E1  遍历方式 A/B：TreeWalker 逐属性读  vs  FindAllBuildCache 一次取回
  E2  视图差异：ControlView  vs  ContentView（节点数 / 耗时 / 序列化字符数）
  E3  读文本：Name / Value / TextPattern 能拿到多少可用字符

用法：
  python scripts/exp_uia_cache.py                 # 前台窗口，E1+E2+E3
  python scripts/exp_uia_cache.py --all           # 所有可见窗口
  python scripts/exp_uia_cache.py --setvalue      # 追加 E4：ValuePattern 直接写值
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import probe_uia as p  # noqa: E402  复用 vtable 调用与属性读取

# IUIAutomation 上额外需要的成员（索引见 UIAutomationClient.h 的声明顺序）
UIA_CREATE_CACHE_REQUEST = 20
UIA_CREATE_TRUE_CONDITION = 21
UIA_GET_CONTROL_VIEW_CONDITION = 18
UIA_GET_CONTENT_VIEW_CONDITION = 19
UIA_ELEMENT_FROM_HANDLE_BUILD_CACHE = 10

# IUIAutomationCacheRequest
CR_ADD_PROPERTY = 3
CR_PUT_TREESCOPE = 7
CR_PUT_TREEFILTER = 9

# IUIAutomationElement 的缓存读 + 批量查找
ELEM_FIND_ALL_BUILD_CACHE = 8
ELEM_GET_CACHED_PROPERTY = 12

# IUIAutomationElementArray
ARR_LENGTH = 3
ARR_GET_ELEMENT = 4

TREESCOPE_ELEMENT = 0x1
TREESCOPE_DESCENDANTS = 0x4
TREESCOPE_SUBTREE = 0x7

CACHE_PROPS = [
    p.P_CONTROL_TYPE, p.P_NAME, p.P_BOUNDING_RECT, p.P_IS_ENABLED,
    p.P_IS_OFFSCREEN, p.P_AUTOMATION_ID, p.P_CLASS_NAME, p.P_IS_KEYBOARD_FOCUSABLE,
    p.P_VALUE,
]

_hr_errors: list[str] = []


def hr(ok: int, what: str) -> bool:
    if ok != 0:
        _hr_errors.append(f"{what} hr=0x{ok & 0xFFFFFFFF:08X}")
        return False
    return True


# --------------------------------------------------------------------------- 缓存路径
def make_cache_request(uia: int) -> int:
    cr = ctypes.c_void_p()
    if not hr(
        p._call(uia, UIA_CREATE_CACHE_REQUEST, ctypes.c_long,
                [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(cr)),
        "CreateCacheRequest",
    ):
        return 0
    for pid in CACHE_PROPS:
        p._call(cr.value, CR_ADD_PROPERTY, ctypes.c_long, [ctypes.c_int], pid)
    # 注意：不要在这里 put_TreeScope。缓存请求的默认 scope 就是 Element —— 即
    # "只缓存 FindAll 返回的每个元素自身"，这正是我们要的。搜索范围由
    # FindAllBuildCache 的 scope 参数控制。实测给 cache request 设 scope 会让
    # 后续 FindAllBuildCache 直接返回 E_FAIL（0x80004005），且不告诉你为什么。
    return cr.value


def view_condition(uia: int, index: int) -> int:
    cond = ctypes.c_void_p()
    p._call(uia, index, ctypes.c_long, [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(cond))
    return cond.value or 0


def cached_walk(uia: int, hwnd: int, *, content: bool) -> tuple[list[dict], float]:
    """一次跨进程调用取回整棵子树的属性。"""
    started = time.perf_counter()
    cr = make_cache_request(uia)
    if not cr:
        return [], 0.0
    cond = view_condition(uia, UIA_GET_CONTENT_VIEW_CONDITION if content
                          else UIA_GET_CONTROL_VIEW_CONDITION)

    root = ctypes.c_void_p()
    if not hr(
        p._call(uia, UIA_ELEMENT_FROM_HANDLE_BUILD_CACHE, ctypes.c_long,
                [wt.HWND, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
                wt.HWND(hwnd), ctypes.c_void_p(cr), ctypes.byref(root)),
        "ElementFromHandleBuildCache",
    ):
        return [], 0.0

    arr = ctypes.c_void_p()
    if not hr(
        p._call(root.value, ELEM_FIND_ALL_BUILD_CACHE, ctypes.c_long,
                [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                 ctypes.POINTER(ctypes.c_void_p)],
                TREESCOPE_SUBTREE, ctypes.c_void_p(cond), ctypes.c_void_p(cr),
                ctypes.byref(arr)),
        "FindAllBuildCache",
    ):
        return [], 0.0

    rows: list[dict] = []
    if arr.value:
        n = ctypes.c_int(0)
        p._call(arr.value, ARR_LENGTH, ctypes.c_long, [ctypes.POINTER(ctypes.c_int)],
                ctypes.byref(n))
        for i in range(n.value):
            el = ctypes.c_void_p()
            ok = p._call(arr.value, ARR_GET_ELEMENT, ctypes.c_long,
                         [ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)],
                         i, ctypes.byref(el))
            if ok != 0 or not el.value:
                continue
            rows.append(read_cached(el.value))
    elapsed = (time.perf_counter() - started) * 1000
    return rows, elapsed


def read_cached(elem: int) -> dict:
    """从缓存里读，无跨进程往返。"""
    def int_prop(pid: int) -> int:
        v = p.Variant()
        p._call(elem, ELEM_GET_CACHED_PROPERTY, ctypes.c_long,
                [ctypes.c_int, ctypes.POINTER(p.Variant)], pid, ctypes.byref(v))
        return int(v.u.lVal) if v.vt == p.VT_I4 else -1

    def str_prop(pid: int) -> str:
        v = p.Variant()
        p._call(elem, ELEM_GET_CACHED_PROPERTY, ctypes.c_long,
                [ctypes.c_int, ctypes.POINTER(p.Variant)], pid, ctypes.byref(v))
        if v.vt != p.VT_BSTR or not v.u.bstrVal:
            return ""
        s = ctypes.wstring_at(v.u.bstrVal)
        p.oleaut32.SysFreeString(ctypes.c_void_p(v.u.bstrVal))
        return s

    def bool_prop(pid: int) -> bool:
        v = p.Variant()
        p._call(elem, ELEM_GET_CACHED_PROPERTY, ctypes.c_long,
                [ctypes.c_int, ctypes.POINTER(p.Variant)], pid, ctypes.byref(v))
        return bool(v.vt == p.VT_BOOL and v.u.boolVal)

    def rect() -> tuple[float, float, float, float] | None:
        v = p.Variant()
        p._call(elem, ELEM_GET_CACHED_PROPERTY, ctypes.c_long,
                [ctypes.c_int, ctypes.POINTER(p.Variant)], p.P_BOUNDING_RECT,
                ctypes.byref(v))
        if v.vt != (p.VT_ARRAY | p.VT_R8) or not v.u.pArray:
            return None
        vals = (ctypes.c_double * 4)()
        for i in range(4):
            idx = ctypes.c_long(i)
            p.oleaut32.SafeArrayGetElement(ctypes.c_void_p(v.u.pArray),
                                           ctypes.byref(idx), ctypes.byref(vals, i * 8))
        p.oleaut32.SafeArrayDestroy(ctypes.c_void_p(v.u.pArray))
        l, t, w, h = vals
        if w <= 0 or h <= 0:
            return None
        return (float(l), float(t), float(w), float(h))

    return {
        "role": p.CONTROL_TYPE_NAMES.get(int_prop(p.P_CONTROL_TYPE), "?"),
        "name": str_prop(p.P_NAME),
        "value": str_prop(p.P_VALUE),
        "rect": rect(),
        "offscreen": bool_prop(p.P_IS_OFFSCREEN),
    }


# --------------------------------------------------------------------------- 指标
def serialized(rows: list[dict]) -> int:
    parts = []
    for i, r in enumerate(rows):
        c = r["rect"]
        cx, cy = (int(c[0] + c[2] / 2), int(c[1] + c[3] / 2)) if c else (0, 0)
        parts.append(f'{i}:{r["role"]} "{r["name"]}" @{cx},{cy}')
    return len("\n".join(parts))


def naive_walk(uia: int, hwnd: int, *, content: bool, max_nodes: int) -> tuple[list, float]:
    return p.walk(uia, hwnd, max_nodes=max_nodes, max_depth=32, content_view=content)


def as_dicts(rows) -> list[dict]:
    return [{"role": r.role, "name": r.name, "value": r.value, "rect": r.rect,
             "offscreen": r.offscreen} for r in rows]


# --------------------------------------------------------------------------- E4
class GUID(ctypes.Structure):
    _fields_ = [
        ("d1", ctypes.c_ulong), ("d2", ctypes.c_ushort),
        ("d3", ctypes.c_ushort), ("d4", ctypes.c_ubyte * 8),
    ]


def guid(text: str) -> GUID:
    g = GUID()
    p.ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(g))
    return g


def find_edit(uia: int, hwnd: int, max_depth: int = 6) -> int | None:
    """BFS 找第一个 Edit 控件（避开 FindFirst 需要的 Condition COM 调用）。"""
    root = p.elem_from_handle(uia, hwnd)
    if not root:
        return None
    walker = p.view_walker(uia, p.UIA_GET_CONTROL_VIEW_WALKER)
    frontier = [(root, 0)]
    while frontier:
        node, depth = frontier.pop(0)
        if p.prop_int(node, p.P_CONTROL_TYPE) == 50004:  # Edit
            return node
        if depth >= max_depth:
            continue
        kid = p.child(walker, node)
        while kid:
            frontier.append((kid, depth + 1))
            kid = p.sibling(walker, kid)
    return None


IID_VALUE_PATTERN = "{A94CD8B1-0844-4CD6-9D2D-640537AB39E9}"
# IUIAutomationValuePattern 的 vtable：3 SetValue / 4 get_CurrentValue /
# 5 get_CurrentIsReadOnly / 6 get_CachedValue / 7 get_CachedIsReadOnly
VP_SET_VALUE = 3
VP_IS_READONLY = 5


def experiment_setvalue() -> None:
    """用 Win+R 运行框做 ValuePattern 写入实验：零键盘注入写入中文并读回。

    选运行框的理由：它是真正的 Win32 Edit（有 ValuePattern），且状态天然临时——
    写入后按 Esc 关闭即可，不污染任何文档。
    """
    u = p.user32
    print("\n=== E4  ValuePattern.SetValue（零键盘注入，绕开输入法）===")
    u.keybd_event(0x5B, 0, 0, 0)   # VK_LWIN
    u.keybd_event(0x52, 0, 0, 0)   # R
    u.keybd_event(0x52, 0, 2, 0)
    u.keybd_event(0x5B, 0, 2, 0)
    time.sleep(1.0)
    try:
        hwnd = None
        for h, title, cls in p.visible_windows():
            if cls == "#32770" and "运行" in title:
                hwnd = h
        if not hwnd:
            print("  ！没抓到运行框")
            return
        uia = p.create_uia()
        root = p.elem_from_handle(uia, hwnd)
        walker = p.view_walker(uia, p.UIA_GET_CONTROL_VIEW_WALKER)
        target, depth = None, 0
        frontier = [(root, 0)]
        while frontier:
            node, depth = frontier.pop(0)
            if depth == 2 and p.prop_int(node, p.P_CONTROL_TYPE) == 50004:  # Edit
                target = node
                break
            if depth >= 4:
                continue
            kid = p.child(walker, node)
            while kid:
                frontier.append((kid, depth + 1))
                kid = p.sibling(walker, kid)
        if not target:
            print("  ！运行框里没找到 Edit")
            return
        print(f"  找到 Edit（深度 {depth}）IsValuePatternAvailable="
              f"{p.prop_bool(target, 30043)}")

        iid = guid(IID_VALUE_PATTERN)
        pat = ctypes.c_void_p()
        ok = p._call(target, p.ELEM_CURRENT_PATTERN, ctypes.c_long,
                     [ctypes.c_int, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)],
                     10002, ctypes.byref(iid), ctypes.byref(pat))
        if ok != 0 or not pat.value:
            print(f"  ！GetCurrentPatternAs(Value) 失败 hr=0x{ok & 0xFFFFFFFF:08X}")
            return
        ro = ctypes.c_short(0)
        p._call(pat.value, VP_IS_READONLY, ctypes.c_long,
                [ctypes.POINTER(ctypes.c_short)], ctypes.byref(ro))
        print(f"  IsReadOnly={bool(ro.value)}")

        text = "UIA SetValue 测试：中文与符号 ￥%&*()"
        r = p._call(pat.value, VP_SET_VALUE, ctypes.c_long, [ctypes.c_wchar_p], text)
        got = p.prop_str(target, p.P_VALUE)
        print(f"  SetValue hr=0x{r & 0xFFFFFFFF:08X}")
        print(f"  读回 = {got!r}  {'[一致]' if got == text else '[不一致]'}")
    finally:
        u.keybd_event(0x1B, 0, 0, 0)   # Esc 关闭运行框
        u.keybd_event(0x1B, 0, 2, 0)


# --------------------------------------------------------------------------- main
def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--hwnd", type=int, default=0)
    ap.add_argument("--max-nodes", type=int, default=600)
    ap.add_argument("--setvalue", action="store_true")
    args = ap.parse_args(argv)

    p._com_ready()
    uia = p.create_uia()
    print("E1/E2/E3 —— 遍历方式、视图、文本可得性（per-monitor V2 已声明时坐标即物理像素）\n")

    targets: list[tuple[int, str]] = []
    if args.hwnd:
        targets = [(args.hwnd, f"hwnd={args.hwnd}")]
    elif args.all:
        targets = [(h, f"{t}  [{c}]") for h, t, c in p.visible_windows()][:10]
    else:
        h = int(p.user32.GetForegroundWindow() or 0)
        targets = [(h, f"前台 hwnd={h}")]

    print(f"{'窗口':38s} {'方式':16s} {'节点':>5s} {'耗时ms':>7s} {'字符':>6s}")
    print("-" * 78)
    for hwnd, label in targets:
        short = label[:36]
        for mode in ("control", "content"):
            t0 = time.perf_counter()
            nrows, nt = naive_walk(uia, hwnd, content=(mode == "content"),
                                   max_nodes=args.max_nodes)
            _ = time.perf_counter() - t0
            crows, ct = cached_walk(uia, hwnd, content=(mode == "content"))
            if not crows and not nrows:
                continue
            print(f"{short:38s} {'A 逐属性 '+mode:16s} {len(nrows):5d} {nt:7.0f} "
                  f"{serialized(as_dicts(nrows)):6d}")
            print(f"{'':38s} {'B 缓存 '+mode:16s} {len(crows):5d} {ct:7.0f} "
                  f"{serialized(crows):6d}")
            if nt > 0 and ct > 0:
                print(f"{'':38s} {'→ 加速':16s} {'':5s} {nt / ct:6.1f}x")

        # E3：文本可得性（用缓存行统计）
        rows = cached_walk(uia, hwnd, content=True)[0] or as_dicts(
            naive_walk(uia, hwnd, content=True, max_nodes=args.max_nodes)[0]
        )
        named = sum(len(r["name"]) for r in rows if r["name"])
        valued = sum(len(r["value"]) for r in rows if r["value"])
        print(f"{'':38s} {'E3 文本量':16s} name={named} 字 / value={valued} 字 "
              f"/ 命名率 {sum(1 for r in rows if r['name']) * 100 // max(1, len(rows))}%")
        print()

    if _hr_errors:
        print("HRESULT 失败：" + "; ".join(sorted(set(_hr_errors))))

    if args.setvalue:
        experiment_setvalue()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
