"""M-FS 文件系统工具集：Read / Write / Edit / DeleteFile / Glob / Grep。

契约：`docs/ENGINEERING_CONTRACT.md` §7.1（通用约定）与 §7.3（文件工具）。

设计约束（照契约执行）：
- 只操作 `workspace_root` 内的路径；越界返回 `error_class="path_outside_workspace"`；
- 失败不抛异常，统一返回 `contracts.tool_error(...)`；
- 工具无状态：不缓存、不记忆上次读写；
- Write / Edit 走"同目录临时文件 + `os.replace`"，避免写到一半的内容；
- 不新增 ListDir / SearchFiles 等同义工具。

超长处理：
- `Read` 单次返回超过 64 KB 时，把本次窗口完整写入
  `tool_outputs/read_<ulid>.txt`，正文只留头 30 + 尾 60 行与统计（同 §7.2 的 stub 规则）；
- `Glob` 结果按 mtime 新→旧排序，默认最多 200 条，截断时在列表末尾追加一条
  `[已截断] ...` 提示行（契约要求返回 `list[str]`，因此提示也以字符串形式出现）；
- `Grep` 默认最多 200 条，截断时末尾追加 `[已截断] ...`。
"""

from __future__ import annotations

import base64
import fnmatch
import heapq
import os
import re
from pathlib import Path
from typing import Any, Literal

from langchain_core.tools import BaseTool, tool

from contracts import tool_error, ulid
from tools.paths import (
    EXCLUDED_DIR_NAMES,
    has_excluded_part,
    relative_display,
    resolve_in_workspace,
)

# ---------------------------------------------------------------- 限额常量
# 图片：base64 后体积 ×4/3，4 MB 原图约 5.3 MB 文本，已是单次工具结果的合理上限
MAX_IMAGE_BYTES = 4 * 1024 * 1024
# Read 单次返回窗口的字节上限（超出则落盘 + stub）
READ_MAX_BYTES = 64 * 1024
# 超过该大小的文本文件走流式读取（不整体读入内存做编码探测）
READ_FULL_DECODE_BYTES = 8 * 1024 * 1024
# Grep 单文件上限与单行展示上限
GREP_MAX_FILE_BYTES = 2 * 1024 * 1024
GREP_MAX_LINE_CHARS = 400
# Glob / Grep 的 limit 硬上限，避免一次吐回海量结果
MAX_LIMIT = 2000
_BINARY_PROBE_BYTES = 4096

IMAGE_EXTS: frozenset[str] = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})
MIME_BY_EXT: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}

_WRITE_HINT = "请用相对工作区根或工作区内绝对路径重试；工作区外文件不允许读写。"
_OUTSIDE_HINT = "路径必须位于 workspace_root 之内（不允许 ../ 或符号链接逃逸）。"


# ---------------------------------------------------------------- 内部工具函数
def _decode_bytes(data: bytes) -> tuple[str, str]:
    """解码并回传实际编码，供 Edit 原样写回。

    顺序：带 BOM 的 UTF-8 → UTF-8 → GBK → UTF-8(replace)。
    `utf-8-sig` 只在原文确实有 BOM 时使用，否则写回会凭空多出 BOM。
    """
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig"), "utf-8-sig"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("gbk"), "gbk"
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace"), "utf-8"


def _looks_binary(path: Path) -> bool:
    """探测前 4 KB 是否含 NUL 字节（二进制文件的朴素判据）。"""
    try:
        with open(path, "rb") as fh:
            return b"\x00" in fh.read(_BINARY_PROBE_BYTES)
    except OSError:
        return True


def _atomic_write(path: Path, content: str, encoding: str = "utf-8") -> None:
    """同目录临时文件 + os.replace；`newline=""` 防止 Windows 把 \\n 改写成 \\r\\n。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{ulid()}.tmp"
    try:
        with open(tmp, "w", encoding=encoding, newline="") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _write_artifact(outputs_dir: Path, prefix: str, content: str) -> Path:
    """把完整内容落到 tool_outputs 目录，返回绝对路径。"""
    outputs_dir.mkdir(parents=True, exist_ok=True)
    target = outputs_dir / f"{prefix}_{ulid()}.txt"
    with open(target, "w", encoding="utf-8", newline="") as fh:
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())
    return target


def _stub(head: list[str], tail: list[str], *, stats: list[str], artifact: Path) -> str:
    """长输出 stub：头 30 + 尾 60 + 统计 + 绝对路径 + 后续建议。"""
    parts = list(stats)
    parts.append("【头 30 行】")
    parts.append("".join(head))
    parts.append("【...中间已省略...】")
    parts.append("【尾 60 行】")
    parts.append("".join(tail))
    parts.append(f"【完整内容已落盘】{artifact}")
    parts.append("【提示】用 Read 的 offset/limit 分页读取，或对该落盘文件用 Grep 定位关键行。")
    return "\n".join(parts)


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(int(value), high))


def _count_occurrence_lines(text: str, needle: str, max_items: int = 20) -> list[int]:
    """返回 needle 每次出现的行号（1 起），最多 max_items 个。"""
    lines: list[int] = []
    start = 0
    while len(lines) < max_items:
        idx = text.find(needle, start)
        if idx < 0:
            break
        lines.append(text.count("\n", 0, idx) + 1)
        start = idx + 1
    return lines


def _walk_files(root: Path):
    """遍历 root 下的文件，剪掉排除目录。"""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIR_NAMES]
        for name in filenames:
            yield Path(dirpath) / name


def _glob_pattern_error(pattern: str) -> dict[str, Any]:
    return tool_error(
        "invalid_pattern",
        f"glob 模式非法: {pattern!r}",
        hint="pattern 必须是相对搜索根的 glob，例如 **/*.py 或 src/**/test_*.py",
    )


# ---------------------------------------------------------------- 工厂
def build_fs_tools(
    *,
    workspace_root: Path,
    tool_outputs_dir: Path,
    max_read_lines: int = 2000,
) -> list[BaseTool]:
    """构造文件工具集，顺序固定为 [Read, Write, Edit, DeleteFile, Glob, Grep]。

    参数均由装配器注入（`tools.yaml` 里的 `{workspace_root}` / `{tool_outputs_dir}`）。
    """
    root = Path(workspace_root).expanduser().resolve()
    outputs_dir = Path(tool_outputs_dir).expanduser()
    read_cap = max(1, int(max_read_lines))

    # ------------------------------------------------------------ Read
    @tool("Read")
    def read_tool(
        path: str,
        offset: int = 0,
        limit: int = 2000,
        as_: Literal["auto", "text", "image"] = "auto",
    ) -> Any:
        """读取工作区内的文件内容。

        无状态：每次从磁盘读取，不缓存。

        入参：
        - path: 工作区内路径（绝对路径，或相对 workspace_root 的相对路径）
        - offset: 起始行号，0 起（默认 0）
        - limit: 最多读取多少行（默认 2000，受装配时的 max_read_lines 限制）
        - as_: auto=按扩展名判断；text=强制按文本读；image=强制按图片读

        返回：
        - 文本文件：字符串（原文，不含行号）；被截断时结尾附 `[已截断]` 说明；
        - 图片文件（png/jpg/jpeg/gif/webp/bmp）：内容块列表
          `[{"type":"text",...}, {"type":"image_url","image_url":{"url":"data:<mime>;base64,..."}}]`，
          可直接被多模态模型消费；超过 4 MB 返回 error_class="image_too_large"；
        - 失败：`{"ok": false, "error_class": ..., "message": ..., "hint": ...}`
          （path_outside_workspace / not_found / is_directory / binary_file /
          offset_out_of_range / image_too_large / not_an_image）
        """
        try:
            target = resolve_in_workspace(path, root)
        except ValueError as exc:
            return tool_error("path_outside_workspace", str(exc), hint=_OUTSIDE_HINT)
        if not target.exists():
            return tool_error(
                "not_found",
                f"文件不存在: {target}",
                hint="用 Glob 确认路径；本工具只读文件，不列目录。",
            )
        if target.is_dir():
            return tool_error(
                "is_directory",
                f"这是目录，不是文件: {target}",
                hint="用 Glob 列出目录下的文件。",
            )

        size = target.stat().st_size
        ext = target.suffix.lower()

        # ---- 图片分支
        if as_ != "text":
            if as_ == "image" or ext in IMAGE_EXTS:
                if ext not in MIME_BY_EXT:
                    return tool_error(
                        "not_an_image",
                        f"不支持的图片格式: {ext or '(无扩展名)'}",
                        hint=f"支持 {', '.join(sorted(IMAGE_EXTS))}",
                    )
                if size > MAX_IMAGE_BYTES:
                    return tool_error(
                        "image_too_large",
                        f"图片 {size} 字节，超过上限 {MAX_IMAGE_BYTES} 字节",
                        hint="先缩小图片尺寸或压缩后再读；本工具不做自动缩放。",
                    )
                try:
                    data = target.read_bytes()
                except OSError as exc:
                    return tool_error("read_failed", f"读取图片失败: {exc}")
                mime = MIME_BY_EXT[ext]
                b64 = base64.b64encode(data).decode("ascii")
                return [
                    {
                        "type": "text",
                        "text": f"[图片] {target} | {mime} | {size} 字节",
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}"},
                    },
                ]

        # ---- 文本分支
        if size == 0:
            return ""
        if _looks_binary(target):
            return tool_error(
                "binary_file",
                f"疑似二进制文件，无法按文本读取: {target}",
                hint="用 Shell 处理二进制；Read 只返回文本或图片。",
            )

        if size <= READ_FULL_DECODE_BYTES:
            try:
                text, _enc = _decode_bytes(target.read_bytes())
            except OSError as exc:
                return tool_error("read_failed", f"读取失败: {exc}")
        else:
            try:
                with open(target, "r", encoding="utf-8", errors="replace", newline="") as fh:
                    text = fh.read()
            except OSError as exc:
                return tool_error("read_failed", f"读取失败: {exc}")

        lines = text.splitlines(keepends=True)
        total = len(lines)
        start = max(0, int(offset))
        if start >= total:
            return tool_error(
                "offset_out_of_range",
                f"offset={start} 超过文件总行数 {total}",
                hint="offset 从 0 起；用更小的 offset 重试。",
            )
        count = _clamp(limit, 1, read_cap)
        window = lines[start : start + count]
        body = "".join(window)
        consumed = start + len(window)
        truncated = consumed < total

        # 单次窗口过大 → 落盘 + stub（契约 §7.2 的同一套规则）
        if len(body.encode("utf-8", errors="replace")) > READ_MAX_BYTES:
            artifact = _write_artifact(outputs_dir, "read", body)
            head = window[:30]
            tail = window[-60:] if len(window) > 90 else []
            stats = [
                f"【文件】{target}",
                f"【统计】总行数 {total}；本次窗口 第 {start + 1}-{consumed} 行；"
                f"窗口 {len(window)} 行 / {len(body.encode('utf-8', errors='replace'))} 字节",
            ]
            return _stub(head, tail, stats=stats, artifact=artifact)

        if truncated:
            body += (
                f"\n\n[已截断] 文件共 {total} 行；本次返回第 {start + 1}-{consumed} 行。"
                f"继续读取请用 offset={consumed}。"
            )
        return body

    # ------------------------------------------------------------ Write
    @tool("Write")
    def write_tool(path: str, content: str) -> dict[str, Any]:
        """写入（覆盖）工作区内的文本文件。

        无状态：整文件覆盖，不做追加；父目录不存在会自动创建；
        写入走"同目录临时文件 + os.replace"，不会留下半截文件。

        入参：
        - path: 工作区内路径（绝对路径，或相对 workspace_root 的相对路径）
        - content: 完整文件内容（UTF-8 写入，保留 \\n 不转 \\r\\n）

        返回：`{"ok": true, "bytes": 写入字节数, "path": 绝对路径}`；
        失败返回 `{"ok": false, "error_class": ..., "message": ..., "hint": ...}`
        （path_outside_workspace / is_directory / write_failed）。
        """
        try:
            target = resolve_in_workspace(path, root)
        except ValueError as exc:
            return tool_error("path_outside_workspace", str(exc), hint=_OUTSIDE_HINT)
        if target.exists() and target.is_dir():
            return tool_error(
                "is_directory",
                f"目标是目录，不能写入: {target}",
                hint="先选一个文件名，或改用 DeleteFile 删除文件。",
            )
        data = content if isinstance(content, str) else str(content)
        try:
            _atomic_write(target, data)
        except OSError as exc:
            return tool_error("write_failed", f"写入失败: {exc}", hint="检查路径是否可写、磁盘是否已满。")
        return {"ok": True, "bytes": len(data.encode("utf-8")), "path": str(target)}

    # ------------------------------------------------------------ Edit
    @tool("Edit")
    def edit_tool(
        path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> dict[str, Any]:
        """对工作区内文本文件做精确字符串替换。

        无状态：每次读盘 → 替换 → 原子写回，不做模糊匹配、不忽略缩进/换行。

        入参：
        - path: 工作区内路径
        - old_string: 要被替换的原文，必须与文件内容**逐字符**一致（含缩进与换行）
        - new_string: 替换后的文本
        - replace_all: false（默认）时要求 old_string 在文件中唯一；true 时全部替换

        返回：`{"ok": true, "replacements": n}`；
        失败返回 `{"ok": false, "error_class": ..., ...}`：
        - not_unique：old_string 出现多次（附 occurrences 行号），需加长上下文或传 replace_all
        - no_match：old_string 不存在（附所在文件路径），先用 Read/Grep 核对原文
        - path_outside_workspace / not_found / is_directory / encoding_error / write_failed
        """
        try:
            target = resolve_in_workspace(path, root)
        except ValueError as exc:
            return tool_error("path_outside_workspace", str(exc), hint=_OUTSIDE_HINT)
        if not target.exists():
            return tool_error("not_found", f"文件不存在: {target}", hint="用 Glob 确认路径。")
        if target.is_dir():
            return tool_error("is_directory", f"这是目录，不是文件: {target}")
        if not isinstance(old_string, str) or old_string == "":
            return tool_error(
                "invalid_args",
                "old_string 不能为空",
                hint="传入文件中真实存在的一段原文。",
            )
        if old_string == new_string:
            return tool_error(
                "invalid_args",
                "old_string 与 new_string 相同，没有可替换内容",
                hint="确认要改成什么。",
            )
        try:
            raw = target.read_bytes()
        except OSError as exc:
            return tool_error("read_failed", f"读取失败: {exc}")
        try:
            text, encoding = _decode_bytes(raw)
        except UnicodeDecodeError as exc:  # 理论上 gbk 已兜底，保留防御
            return tool_error("encoding_error", f"无法解码文件: {exc}", hint="用 Shell 处理该编码的文件。")

        occurrences = text.count(old_string)
        if occurrences == 0:
            return tool_error(
                "no_match",
                f"old_string 在 {target} 中不存在",
                hint="先用 Read 查看原文，或用 Grep 定位；注意缩进、引号、换行必须完全一致。",
            )
        if occurrences > 1 and not replace_all:
            lines = _count_occurrence_lines(text, old_string)
            return tool_error(
                "not_unique",
                f"old_string 在 {target} 中出现 {occurrences} 次，无法确定替换哪一处",
                hint="加长上下文使其唯一，或传 replace_all=true 全部替换。",
                occurrences=lines,
            )

        replacements = occurrences if replace_all else 1
        updated = text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1)
        try:
            _atomic_write(target, updated, encoding=encoding)
        except OSError as exc:
            return tool_error("write_failed", f"写回失败: {exc}", hint="检查路径是否可写。")
        return {"ok": True, "replacements": replacements, "path": str(target)}

    # ------------------------------------------------------------ DeleteFile
    @tool("DeleteFile")
    def delete_file_tool(paths: list[str] | str) -> dict[str, Any]:
        """删除工作区内的一个或多个文件。

        无状态：只删文件，不删目录、不递归；不存在的路径记为 failed 而不是抛异常。

        入参：
        - paths: 路径列表（也接受单个字符串）

        返回：`{"deleted": [绝对路径...], "failed": [{"path": 原始入参, "reason": 稳定错误码}]}`
        reason ∈ {path_outside_workspace, not_found, is_directory, permission, delete_failed}
        """
        items = [paths] if isinstance(paths, str) else list(paths or [])
        deleted: list[str] = []
        failed: list[dict[str, Any]] = []
        for raw_path in items:
            try:
                target = resolve_in_workspace(raw_path, root)
            except ValueError:
                failed.append({"path": str(raw_path), "reason": "path_outside_workspace"})
                continue
            if not target.exists():
                failed.append({"path": str(raw_path), "reason": "not_found"})
                continue
            if target.is_dir():
                failed.append({"path": str(raw_path), "reason": "is_directory"})
                continue
            try:
                target.unlink()
            except PermissionError:
                failed.append({"path": str(raw_path), "reason": "permission"})
            except OSError:
                failed.append({"path": str(raw_path), "reason": "delete_failed"})
            else:
                deleted.append(str(target))
        return {"deleted": deleted, "failed": failed}

    # ------------------------------------------------------------ Glob
    @tool("Glob")
    def glob_tool(pattern: str, path: str | None = None, limit: int = 200) -> Any:
        """按 glob 模式查找工作区内的文件，按修改时间新→旧返回。

        无状态：每次实时遍历磁盘；结果只含文件，不含目录；
        始终排除 `.git` / `node_modules` / `.venv` / `__pycache__` 目录。

        入参：
        - pattern: 相对搜索根的 glob，支持 `**`（如 `**/*.py`、`src/**/test_*.py`）
        - path: 搜索根（工作区内绝对路径或相对工作区路径）；默认 workspace_root
        - limit: 最多返回条数（默认 200，硬上限 2000）

        返回：`list[str]` 绝对路径，最新修改的在前；
        - 无匹配：`["[no matches] ..."]`；
        - 截断：末尾追加 `"[已截断] ..."` 提示行；
        - 参数/路径错误：`{"ok": false, "error_class": ...}`。
        """
        try:
            search_root = resolve_in_workspace(path, root) if path else root
        except ValueError as exc:
            return tool_error("path_outside_workspace", str(exc), hint=_OUTSIDE_HINT)
        if not search_root.exists():
            return tool_error("not_found", f"搜索根不存在: {search_root}", hint="用 workspace_root 或已存在目录。")
        if not search_root.is_dir():
            return tool_error("not_a_directory", f"搜索根不是目录: {search_root}")

        if not isinstance(pattern, str) or not pattern.strip():
            return _glob_pattern_error(pattern)

        pat = pattern.replace("\\", "/").strip()
        try:
            if Path(pat).is_absolute():
                abs_pat = resolve_in_workspace(pat, root)
                pat = abs_pat.relative_to(root).as_posix()
            matches = search_root.glob(pat)
            keep_n = _clamp(limit, 1, MAX_LIMIT) + 1
            heap: list[tuple[float, str]] = []
            total = 0
            for item in matches:
                try:
                    if not item.is_file():
                        continue
                except OSError:
                    continue
                if has_excluded_part(item, root):
                    continue
                total += 1
                try:
                    mtime = item.stat().st_mtime
                except OSError:
                    mtime = 0.0
                entry = (mtime, str(item))
                if len(heap) < keep_n:
                    heapq.heappush(heap, entry)
                elif entry[0] > heap[0][0]:
                    heapq.heapreplace(heap, entry)
        except ValueError:
            return _glob_pattern_error(pattern)
        except OSError as exc:
            return tool_error("glob_failed", f"遍历目录失败: {exc}")

        effective = _clamp(limit, 1, MAX_LIMIT)
        ordered = sorted(heap, key=lambda kv: (-kv[0], kv[1]))
        results = [p for _, p in ordered[:effective]]
        if total == 0:
            return [f"[no matches] pattern={pattern!r} root={search_root} (0 个文件)"]
        if total > effective:
            results.append(
                f"[已截断] 共匹配 {total} 个文件，仅返回最新 {effective} 个；"
                f"用更精确的 pattern 或限定 path 缩小范围。"
            )
        return results

    # ------------------------------------------------------------ Grep
    @tool("Grep")
    def grep_tool(
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        n: bool = True,
        limit: int = 200,
    ) -> Any:
        """在工作区内按正则搜索文件内容（等价 `grep -rn`，参数名 `n` 即 `-n`）。

        无状态：每次实时扫描。始终排除 `.git` / `node_modules` / `.venv` / `__pycache__`；
        跳过二进制文件（前 4 KB 含 NUL）与大于 2 MB 的文件；单行最多展示 400 字符。

        入参：
        - pattern: Python 正则（re.search 语义）
        - path: 搜索根（文件或目录；工作区内）；默认 workspace_root
        - glob: 只搜匹配该 glob 的文件（如 `*.py`，同时匹配相对路径与文件名）
        - n: True 输出 `文件:行号: 内容`；False 输出 `文件: 内容`
        - limit: 最多返回命中行数（默认 200，硬上限 2000）

        返回：多行字符串（每行一条命中）；
        - 无匹配：`"[no matches] ..."`；
        - 截断：末尾追加 `[已截断] ...` 提示行；
        - 正则非法 / 路径错误：`{"ok": false, "error_class": ...}`。
        """
        if not isinstance(pattern, str) or pattern == "":
            return tool_error("invalid_pattern", "pattern 不能为空", hint="传入合法的 Python 正则。")
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return tool_error(
                "invalid_pattern",
                f"正则非法: {exc}",
                hint="转义特殊字符，或换用更简单的模式。",
            )

        try:
            target = resolve_in_workspace(path, root) if path else root
        except ValueError as exc:
            return tool_error("path_outside_workspace", str(exc), hint=_OUTSIDE_HINT)
        if not target.exists():
            return tool_error("not_found", f"搜索路径不存在: {target}", hint="用 Glob 确认路径。")

        if target.is_file():
            files = [target]
        else:
            files = _walk_files(target)

        effective = _clamp(limit, 1, MAX_LIMIT)
        hits: list[str] = []
        skipped_binary = 0
        skipped_large = 0
        was_truncated = False

        glob_pat = glob.strip() if isinstance(glob, str) and glob.strip() else None
        stop = False

        for file_path in files:
            if has_excluded_part(file_path, root):
                continue
            if glob_pat and not (
                fnmatch.fnmatch(file_path.name, glob_pat)
                or fnmatch.fnmatch(relative_display(file_path, root), glob_pat)
            ):
                continue
            try:
                st = file_path.stat()
            except OSError:
                continue
            if st.st_size > GREP_MAX_FILE_BYTES:
                skipped_large += 1
                continue
            if st.st_size == 0:
                continue
            if _looks_binary(file_path):
                skipped_binary += 1
                continue
            try:
                raw = file_path.read_bytes()
            except OSError:
                continue
            text, _enc = _decode_bytes(raw)
            for lineno, line in enumerate(text.splitlines(), start=1):
                if not regex.search(line):
                    continue
                if len(hits) < effective:
                    shown = line.rstrip()
                    if len(shown) > GREP_MAX_LINE_CHARS:
                        shown = shown[:GREP_MAX_LINE_CHARS] + "…"
                    hits.append(f"{file_path}:{lineno}: {shown}" if n else f"{file_path}: {shown}")
                else:
                    was_truncated = True
                    stop = True
                    break
            if stop:
                break

        if not hits:
            skip_note = ""
            if skipped_binary or skipped_large:
                skip_note = f"（已跳过 {skipped_binary} 个二进制、{skipped_large} 个 >2MB 文件）"
            return (
                f"[no matches] pattern={pattern!r} root={target}"
                f"{f' glob={glob_pat!r}' if glob_pat else ''}{skip_note}"
            )

        output = list(hits)
        if was_truncated:
            output.append(
                f"[已截断] 命中数超过上限 {effective} 条；用更精确的 pattern / glob 或限定 path 缩小范围。"
            )
        return "\n".join(output)

    return [read_tool, write_tool, edit_tool, delete_file_tool, glob_tool, grep_tool]
