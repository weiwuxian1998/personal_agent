"""M-FS 模块验收脚本（契约 §10 / §13 第 2 条）。

运行：`python scripts/check_fs.py`，退出码 0 才算交付。

覆盖：
- 工具清单与顺序（Read/Write/Edit/DeleteFile/Glob/Grep）；
- Write/Read 往返（UTF-8、CRLF 保真、嵌套目录自动创建）；
- Read 的 offset/limit、越界 offset、大文件落盘 stub、二进制拒绝、目录拒绝；
- Read 图片：多模态内容块 + data URI 可还原、as_=text、超大图片拒绝；
- 只读白名单（飞书附件目录）：Read/Glob/Grep 放行，Write/Edit/DeleteFile 返回 path_read_only；
- Edit：唯一匹配、无匹配、多处匹配（not_unique）、replace_all；
- DeleteFile：部分成功、缺失、目录、越界；
- 工作区越界（`../`、绝对外部路径、同前缀兄弟目录、符号链接）：读写删/Glob/Grep 全线拦截；
- Glob：排序（mtime 新→旧）、排除 `.git`/`node_modules`/`.venv`/`__pycache__`、空结果、截断；
- Grep：行号、n=False、glob 过滤、非法正则、空结果、截断、跳过二进制与 >2MB 文件。
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.fs import MAX_IMAGE_BYTES, build_fs_tools  # noqa: E402
from tools.paths import resolve_in_workspace  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []

# 1x1 透明 PNG
PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def check(name: str, cond: object, detail: object = "") -> None:
    if cond:
        PASSED.append(name)
        print(f"PASS  {name}")
    else:
        FAILED.append(name)
        print(f"FAIL  {name}  {detail}")


def check_raises(name: str, fn) -> None:
    try:
        fn()
    except ValueError:
        check(name, True)
    except Exception as exc:  # noqa: BLE001
        check(name, False, f"异常类型不对: {type(exc).__name__}: {exc}")
    else:
        check(name, False, "未抛 ValueError")


def main() -> int:
    base = Path(tempfile.mkdtemp(prefix="check_fs_"))
    ws = base / "workspace"
    ws.mkdir()
    outputs = base / "tool_outputs"

    tools = build_fs_tools(workspace_root=ws, tool_outputs_dir=outputs)
    read, write, edit, delete, glob, grep = tools
    check(
        "工具清单与顺序为 Read/Write/Edit/DeleteFile/Glob/Grep",
        [t.name for t in tools] == ["Read", "Write", "Edit", "DeleteFile", "Glob", "Grep"],
        [t.name for t in tools],
    )

    # ---------------------------------------------------------------- 路径守卫
    outside = base / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    sibling = base / "workspace-evil"
    sibling.mkdir()
    (sibling / "x.txt").write_text("x", encoding="utf-8")

    check_raises("resolve_in_workspace 拒绝 ../ 越界", lambda: resolve_in_workspace("..\\outside.txt", ws))
    check_raises(
        "resolve_in_workspace 拒绝绝对外部路径",
        lambda: resolve_in_workspace(str(outside), ws),
    )
    check_raises(
        "resolve_in_workspace 拒绝同前缀兄弟目录",
        lambda: resolve_in_workspace(str(sibling / "x.txt"), ws),
    )

    # ---------------------------------------------------------------- Write / Read 往返
    content = "hello\n中文 line2\n"
    res = write.invoke({"path": "notes/hello.txt", "content": content})
    check(
        "Write 返回 ok/bytes/绝对 path",
        isinstance(res, dict)
        and res.get("ok") is True
        and res.get("bytes") == len(content.encode("utf-8"))
        and Path(res.get("path", "")).is_absolute(),
        res,
    )
    check("Write 自动创建父目录", (ws / "notes" / "hello.txt").is_file())
    check("Read 文本往返一致", read.invoke({"path": "notes/hello.txt"}) == content)

    write.invoke({"path": "notes/crlf.txt", "content": "a\r\nb\r\n"})
    check(
        "Write 不把 \\n 改写成 \\r\\n",
        (ws / "notes" / "crlf.txt").read_bytes() == b"a\r\nb\r\n",
        (ws / "notes" / "crlf.txt").read_bytes(),
    )
    check("Read 保留 CRLF", read.invoke({"path": "notes/crlf.txt"}) == "a\r\nb\r\n")

    ten = "".join(f"line{i}\n" for i in range(10))
    write.invoke({"path": "notes/ten.txt", "content": ten})
    out = read.invoke({"path": "notes/ten.txt", "offset": 2, "limit": 3})
    check("Read offset/limit 取第 3-5 行", isinstance(out, str) and out.startswith("line2\nline3\nline4\n"), out)
    check("Read 截断时提示续读 offset", isinstance(out, str) and "offset=5" in out, out)

    res = read.invoke({"path": "notes/ten.txt", "offset": 99})
    check(
        "Read offset 越界返回 offset_out_of_range",
        isinstance(res, dict) and res.get("error_class") == "offset_out_of_range",
        res,
    )
    res = read.invoke({"path": "notes/nope.txt"})
    check("Read 缺失返回 not_found", isinstance(res, dict) and res.get("error_class") == "not_found", res)
    res = read.invoke({"path": "notes"})
    check("Read 目录返回 is_directory", isinstance(res, dict) and res.get("error_class") == "is_directory", res)

    # ---------------------------------------------------------------- 大文件落盘 stub
    big_content = "".join("x" * 100 + "\n" for _ in range(3000))
    write.invoke({"path": "notes/big.txt", "content": big_content})
    stub = read.invoke({"path": "notes/big.txt"})
    check("Read 大文件返回落盘 stub", isinstance(stub, str) and "完整内容已落盘" in stub, str(stub)[:200])
    check("Read 大文件默认窗口被 max_read_lines 限为 2000 行", "窗口 2000 行" in str(stub), str(stub)[:300])
    match = re.search(r"【完整内容已落盘】(.*)", str(stub))
    artifact = Path(match.group(1).strip()) if match else Path("__missing__")
    check(
        "Read 落盘文件位于 tool_outputs 且内容为 2000 行",
        artifact.is_file()
        and artifact.parent.resolve() == outputs.resolve()
        and len(artifact.read_text(encoding="utf-8").splitlines()) == 2000,
        artifact,
    )

    # ---------------------------------------------------------------- Edit
    write.invoke({"path": "notes/edit.txt", "content": "alpha\nbeta\ngamma beta\n"})
    res = edit.invoke({"path": "notes/edit.txt", "old_string": "alpha", "new_string": "ALPHA"})
    check(
        "Edit 唯一匹配成功",
        isinstance(res, dict) and res.get("ok") is True and res.get("replacements") == 1,
        res,
    )
    check(
        "Edit 结果已原子写回",
        (ws / "notes" / "edit.txt").read_text(encoding="utf-8").startswith("ALPHA\nbeta"),
    )
    res = edit.invoke({"path": "notes/edit.txt", "old_string": "does-not-exist", "new_string": "x"})
    check(
        "Edit 无匹配返回 no_match",
        isinstance(res, dict) and res.get("error_class") == "no_match" and res.get("hint"),
        res,
    )
    res = edit.invoke({"path": "notes/edit.txt", "old_string": "beta", "new_string": "B"})
    check(
        "Edit 多处匹配返回 not_unique 且附行号",
        isinstance(res, dict) and res.get("error_class") == "not_unique" and res.get("occurrences") == [2, 3],
        res,
    )
    res = edit.invoke(
        {"path": "notes/edit.txt", "old_string": "beta", "new_string": "B", "replace_all": True}
    )
    check(
        "Edit replace_all 全部替换",
        isinstance(res, dict) and res.get("ok") is True and res.get("replacements") == 2,
        res,
    )
    check(
        "Edit replace_all 后原文不再存在",
        "beta" not in (ws / "notes" / "edit.txt").read_text(encoding="utf-8"),
    )

    bom_file = ws / "notes" / "bom.txt"
    bom_file.write_bytes(b"\xef\xbb\xbf" + "alpha\n".encode())
    res = edit.invoke({"path": "notes/bom.txt", "old_string": "alpha", "new_string": "beta"})
    check(
        "Edit 保留原有 UTF-8 BOM（不新增也不丢失）",
        isinstance(res, dict) and res.get("ok") is True and bom_file.read_bytes() == b"\xef\xbb\xbfbeta\n",
        bom_file.read_bytes(),
    )

    # ---------------------------------------------------------------- DeleteFile
    write.invoke({"path": "notes/del1.txt", "content": "1"})
    write.invoke({"path": "notes/del2.txt", "content": "2"})
    res = delete.invoke({"paths": ["notes/del1.txt", "notes/del2.txt", "notes/missing.txt"]})
    check(
        "DeleteFile 部分成功写入 deleted",
        isinstance(res, dict) and len(res.get("deleted", [])) == 2 and not (ws / "notes" / "del1.txt").exists(),
        res,
    )
    check(
        "DeleteFile 缺失文件进 failed",
        isinstance(res, dict) and res.get("failed", [{}])[0].get("reason") == "not_found",
        res,
    )
    res = delete.invoke({"paths": [str(ws / "notes")]})
    check(
        "DeleteFile 拒绝目录",
        isinstance(res, dict) and res.get("failed", [{}])[0].get("reason") == "is_directory",
        res,
    )
    res = delete.invoke({"paths": [str(outside)]})
    check(
        "DeleteFile 拒绝工作区外",
        isinstance(res, dict) and res.get("failed", [{}])[0].get("reason") == "path_outside_workspace",
        res,
    )
    check("DeleteFile 未误删工作区外文件", outside.exists())

    # ---------------------------------------------------------------- Glob
    proj = ws / "proj"
    for rel in ("a.py", "sub/b.py", ".git/c.py", "node_modules/d.py", "__pycache__/e.py", ".venv/f.py"):
        target = proj / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# {rel}\n", encoding="utf-8")
    os.utime(proj / "a.py", (1_000_000_000, 1_000_000_000))
    os.utime(proj / "sub" / "b.py", (1_700_000_000, 1_700_000_000))

    res = glob.invoke({"pattern": "**/*.py", "path": "proj"})
    check(
        "Glob 命中文件且排除 .git/node_modules/.venv/__pycache__",
        isinstance(res, list) and len(res) == 2 and all(p.endswith(".py") for p in res),
        res,
    )
    check("Glob 按 mtime 新→旧排序", isinstance(res, list) and Path(res[0]).name == "b.py", res)
    check("Glob 返回绝对路径", isinstance(res, list) and all(Path(p).is_absolute() for p in res), res)

    res = glob.invoke({"pattern": "**/*.rs", "path": "proj"})
    check(
        "Glob 空结果返回提示项",
        isinstance(res, list) and len(res) == 1 and res[0].startswith("[no matches]"),
        res,
    )
    res = glob.invoke({"pattern": "**/*.py", "path": "proj", "limit": 1})
    check(
        "Glob 截断追加提示行",
        isinstance(res, list) and len(res) == 2 and res[-1].startswith("[已截断]"),
        res,
    )

    # ---------------------------------------------------------------- Grep
    (proj / "a.py").write_text("print('TODO a')\n", encoding="utf-8")
    (proj / "sub" / "b.py").write_text("x = 1\nTODO second\n", encoding="utf-8")
    res = grep.invoke({"pattern": "TODO", "path": "proj"})
    check(
        "Grep 基本命中带文件:行号",
        isinstance(res, str) and "a.py:1:" in res and "b.py:2:" in res,
        res,
    )
    res = grep.invoke({"pattern": "TODO", "path": "proj", "n": False})
    check(
        "Grep n=False 不输出行号",
        isinstance(res, str) and "a.py:" in res and "a.py:1:" not in res,
        res,
    )
    res = grep.invoke({"pattern": "TODO", "path": "proj", "glob": "b.py"})
    check(
        "Grep glob 过滤只命中指定文件",
        isinstance(res, str) and "b.py:2:" in res and "a.py" not in res,
        res,
    )
    res = grep.invoke({"pattern": "(", "path": "proj"})
    check(
        "Grep 非法正则返回 invalid_pattern",
        isinstance(res, dict) and res.get("error_class") == "invalid_pattern",
        res,
    )
    res = grep.invoke({"pattern": "ZZZ_NOPE", "path": "proj"})
    check(
        "Grep 空结果返回 no matches",
        isinstance(res, str) and res.startswith("[no matches]"),
        res,
    )
    res = grep.invoke({"pattern": "TODO", "path": "proj", "limit": 1})
    check(
        "Grep 截断追加提示行",
        isinstance(res, str) and "已截断" in res and len(res.splitlines()) == 2,
        res,
    )
    check(
        "Grep 排除 .git 目录",
        str(grep.invoke({"pattern": "# .git/c.py", "path": "proj"})).startswith("[no matches]"),
    )

    bigs = ws / "bigs"
    bigs.mkdir()
    (bigs / "huge.txt").write_text(("y" * 100 + "\n") * 30000 + "TODO_LARGE\n", encoding="utf-8")
    res = grep.invoke({"pattern": "TODO_LARGE", "path": "bigs"})
    check(
        "Grep 跳过 >2MB 文件",
        isinstance(res, str) and "已跳过" in res and "1 个 >2MB" in res,
        res,
    )
    bins = ws / "bins"
    bins.mkdir()
    (bins / "b.dat").write_bytes(b"\x00\x01TODO_BIN")
    res = grep.invoke({"pattern": "TODO_BIN", "path": "bins"})
    check(
        "Grep 跳过二进制文件",
        isinstance(res, str) and "已跳过" in res and "1 个二进制" in res,
        res,
    )
    res = read.invoke({"path": "bins/b.dat"})
    check(
        "Read 二进制返回 binary_file",
        isinstance(res, dict) and res.get("error_class") == "binary_file",
        res,
    )

    # ---------------------------------------------------------------- 图片
    img_dir = ws / "img"
    img_dir.mkdir()
    png = base64.b64decode(PNG_B64)
    (img_dir / "dot.png").write_bytes(png)
    res = read.invoke({"path": "img/dot.png"})
    check(
        "Read 图片只回引用文本（不内联 base64）",
        isinstance(res, list)
        and len(res) == 1
        and res[0].get("type") == "text"
        and res[0]["text"].startswith("[图片] ")
        and "base64" not in res[0]["text"],
        res,
    )
    # 引用 → 请求时还原：图片以独立 user 消息追加（端点限制：tool 消息带图 400）
    from types import SimpleNamespace as _NS

    from agents.vision import inject_image_blocks as _inject

    injected = _inject([_NS(content=list(res))])
    img_blocks = [
        b
        for b in (getattr(injected[-1], "content", None) or [])
        if isinstance(b, dict) and b.get("type") == "image_url"
    ]
    ok_inject = (
        len(injected) == 2
        and len(img_blocks) == 1
        and img_blocks[0]["image_url"]["url"].startswith("data:image/png;base64,")
        and base64.b64decode(img_blocks[0]["image_url"]["url"].split(",", 1)[1]) == png
    )
    check("Read 图片引用在请求时还原为 user 消息里的 image_url 且字节一致", ok_inject, str(injected[-1].content)[:120])

    # BMP 不在端点支持列表（仅 JPEG/PNG/GIF/WebP）：注入时应自动转码为 PNG
    from PIL import Image as _Image
    import io as _io

    _bmp_buf = _io.BytesIO()
    _Image.new("RGB", (4, 4), (255, 0, 0)).save(_bmp_buf, format="BMP")
    (img_dir / "dot.bmp").write_bytes(_bmp_buf.getvalue())
    res = read.invoke({"path": "img/dot.bmp"})
    injected = _inject([_NS(content=list(res))])
    img_blocks = [
        b
        for b in (getattr(injected[-1], "content", None) or [])
        if isinstance(b, dict) and b.get("type") == "image_url"
    ]
    check(
        "Read BMP 引用注入时转码为端点支持格式（非 image/bmp 且可解码）",
        len(injected) == 2
        and len(img_blocks) == 1
        and img_blocks[0]["image_url"]["url"].startswith("data:image/")
        and not img_blocks[0]["image_url"]["url"].startswith("data:image/bmp")
        and _Image.open(_io.BytesIO(base64.b64decode(img_blocks[0]["image_url"]["url"].split(",", 1)[1]))) is not None,
        img_blocks[0]["image_url"]["url"][:40] if img_blocks else str(injected[-1].content)[:120],
    )

    # 数量上限：从最新引用往前数、按路径去重，最多 INJECT_MAX_IMAGES 张
    from agents.vision import INJECT_MAX_IMAGES as _MAXI

    cap_msgs: list = []
    for i in range(_MAXI + 3):
        (img_dir / f"cap_{i}.png").write_bytes(png)
        ref = read.invoke({"path": f"img/cap_{i}.png"})[0]["text"]
        cap_msgs.append(_NS(content=[{"type": "text", "text": ref}]))
    injected = _inject(cap_msgs)
    n_imgs = sum(
        1 for b in (getattr(injected[-1], "content", None) or []) if isinstance(b, dict) and b.get("type") == "image_url"
    )
    check(
        f"图片注入数量上限（最新 {_MAXI} 张优先，其余保持文本）",
        len(injected) == len(cap_msgs) + 1 and n_imgs == _MAXI,
        f"{n_imgs}/{_MAXI}",
    )
    res = read.invoke({"path": "img/dot.png", "as_": "text"})
    check(
        "Read as_=text 时图片按二进制拒绝",
        isinstance(res, dict) and res.get("error_class") == "binary_file",
        res,
    )
    (img_dir / "huge.png").write_bytes(b"\x00" * (MAX_IMAGE_BYTES + 1))
    res = read.invoke({"path": "img/huge.png"})
    check(
        "Read 超大图片返回 image_too_large",
        isinstance(res, dict) and res.get("error_class") == "image_too_large",
        res,
    )

    # ---------------------------------------------------------------- 只读白名单（飞书附件）
    inbox = base / "inbox"
    inbox.mkdir()
    (inbox / "img_abc.png").write_bytes(png)
    (inbox / "报告.txt").write_text("附件正文\n", encoding="utf-8", newline="\n")
    guarded = build_fs_tools(workspace_root=ws, tool_outputs_dir=outputs, read_roots=[inbox])
    g_read, g_write, g_edit, g_delete, g_glob, g_grep = guarded

    res = g_read.invoke({"path": str(inbox / "报告.txt")})
    check("只读白名单 Read 可读附件文本", res == "附件正文\n", res)
    res = g_read.invoke({"path": str(inbox / "img_abc.png")})
    check(
        "只读白名单 Read 图片同样只回引用文本",
        isinstance(res, list)
        and len(res) == 1
        and res[0].get("type") == "text"
        and res[0]["text"].startswith("[图片] ")
        and "base64" not in res[0]["text"],
        str(res)[:120],
    )

    res = g_write.invoke({"path": str(inbox / "new.txt"), "content": "x"})
    check(
        "Write 落附件目录返回 path_read_only 且带 hint",
        isinstance(res, dict)
        and res.get("error_class") == "path_read_only"
        and res.get("hint"),
        res,
    )
    check("Write 没有写进附件目录", not (inbox / "new.txt").exists())
    res = g_write.invoke({"path": "..\\inbox\\escape.txt", "content": "x"})
    check(
        "Write 用 .. 摸进附件目录也返回 path_read_only",
        isinstance(res, dict) and res.get("error_class") == "path_read_only",
        res,
    )
    res = g_edit.invoke({"path": str(inbox / "报告.txt"), "old_string": "附件", "new_string": "x"})
    check(
        "Edit 附件返回 path_read_only",
        isinstance(res, dict) and res.get("error_class") == "path_read_only",
        res,
    )
    res = g_delete.invoke({"paths": [str(inbox / "报告.txt")]})
    check(
        "DeleteFile 附件返回 path_read_only",
        isinstance(res, dict) and res.get("failed", [{}])[0].get("reason") == "path_read_only",
        res,
    )
    check("附件文件未被删除", (inbox / "报告.txt").exists())

    res = g_glob.invoke({"pattern": "*.txt", "path": str(inbox)})
    check(
        "只读白名单 Glob 可列附件",
        isinstance(res, list) and any(str(p).endswith("报告.txt") for p in res),
        res,
    )
    res = g_glob.invoke({"pattern": "**/*.png"})
    check(
        "Glob 默认仍只搜 workspace_root（不主动扫附件目录）",
        isinstance(res, list) and all(str(inbox) not in str(p) for p in res),
        res,
    )
    res = g_grep.invoke({"pattern": "附件正文", "path": str(inbox)})
    check("只读白名单 Grep 可搜附件", isinstance(res, str) and "报告.txt" in res, res)
    res = g_read.invoke({"path": str(outside)})
    check(
        "只读白名单不影响其它越界拦截",
        isinstance(res, dict) and res.get("error_class") == "path_outside_workspace",
        res,
    )

    # ---------------------------------------------------------------- 越界（工具层）
    res = read.invoke({"path": "../outside.txt"})
    check(
        "Read 越界返回 path_outside_workspace",
        isinstance(res, dict) and res.get("error_class") == "path_outside_workspace",
        res,
    )
    res = write.invoke({"path": str(base / "evil.txt"), "content": "x"})
    check(
        "Write 越界被拒",
        isinstance(res, dict) and res.get("error_class") == "path_outside_workspace",
        res,
    )
    check("Write 越界未落盘", not (base / "evil.txt").exists())
    res = glob.invoke({"pattern": "*", "path": ".."})
    check(
        "Glob 越界被拒",
        isinstance(res, dict) and res.get("error_class") == "path_outside_workspace",
        res,
    )
    res = grep.invoke({"pattern": "secret", "path": str(base)})
    check(
        "Grep 越界被拒",
        isinstance(res, dict) and res.get("error_class") == "path_outside_workspace",
        res,
    )
    res = edit.invoke({"path": "../outside.txt", "old_string": "secret", "new_string": "x"})
    check(
        "Edit 越界被拒",
        isinstance(res, dict) and res.get("error_class") == "path_outside_workspace",
        res,
    )

    link = ws / "link_outside.txt"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        print("SKIP  符号链接逃逸（当前账户无法创建符号链接）")
    else:
        res = read.invoke({"path": "link_outside.txt"})
        check(
            "Read 拒绝符号链接逃逸",
            isinstance(res, dict) and res.get("error_class") == "path_outside_workspace",
            res,
        )

    check("Write/Edit 不残留临时文件", not list(ws.rglob("*.tmp")), list(ws.rglob("*.tmp")))

    shutil.rmtree(base, ignore_errors=True)
    print(f"\n== M-FS 验收：{len(PASSED)} 通过 / {len(FAILED)} 失败 ==")
    if FAILED:
        print("失败项：" + "; ".join(FAILED))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        raise SystemExit(2)
