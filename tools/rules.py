"""L3 规则层（设计文档 §12.3.1）：R1–R8 的确定性判断。

**只做确定性判断**：不调用模型，不看"意图"，只解析命令名与参数。
判断原则（文档原文）："只看最终动作——`Get-Help Remove-Item` 不应命中 R2；
用命令名 + 参数解析，而不是全文关键词扫描"。

因此这里先把脚本土按**引号外**的 `;` / 换行 / `&&` / `||` / `|` 切成语句与管道段，
再取每段的首个 token 作为命令名，按规则表匹配。引号内的内容不会被当命令解析
（R7 是唯一的例外：凭据特征本身就藏在字符串里，按文档要求直接匹配）。

对外只暴露 `check_command_rules()`；规则表是常量，不允许 worker 修改。
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Action = Literal["deny", "need_confirm"]

# ---------------------------------------------------------------------------- 区域常量
# 禁区是**唯一允许硬编码的底线**：这些目录永远不可写、不可删，
# 即使被 `play_root` 配置成可玩耍区也一律拒绝。不要把它做成配置项。
# 盘符根只禁"根本身"（否则整块 C: 都不可用，配置 `D:\` 也会被误伤）。
FORBIDDEN_EXACT: tuple[str, ...] = ("C:\\", "/")
FORBIDDEN_SUBTREES: tuple[str, ...] = (
    "C:\\Windows",
    "C:\\Program Files",
    "C:\\Program Files (x86)",
    "C:\\ProgramData",
    "C:\\$Recycle.Bin",
    "C:\\System Volume Information",
    "C:\\Recovery",
    "C:\\PerfLogs",
    "/System",
    "/usr",
    "/etc",
    "/bin",
    "/sbin",
    "/var",
)
# 个人目录：默认只读；递归删除/宽通配一律拒绝。
PERSONAL_DIR_NAMES: tuple[str, ...] = ("Desktop", "Downloads", "Documents")
# 单次删除/移动个人目录文件的上限。
MAX_BATCH_FILES = 10


class PathForbidden(ValueError):
    """路径落在禁区（系统目录）：任何写入/删除都必须拒绝。"""

    error_class = "path_forbidden"


def _resolved(path: str | Path) -> Path | None:
    try:
        return Path(path).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def forbidden_roots() -> list[Path]:
    """禁区目录的真实路径（不存在的自动跳过）。"""
    candidates = list(FORBIDDEN_SUBTREES)
    for env in ("APPDATA", "LOCALAPPDATA", "PROGRAMDATA"):
        value = os.environ.get(env)
        if value:
            candidates.append(value)
    home = Path.home()
    candidates.append(str(home / "AppData"))
    roots: list[Path] = []
    for raw in candidates:
        path = _resolved(raw)
        if path is not None and path.exists() and path not in roots:
            roots.append(path)
    return roots


def forbidden_exact_roots() -> list[Path]:
    """盘符根 / 文件系统根：只禁根本身，不禁其下所有内容。"""
    roots: list[Path] = []
    for raw in FORBIDDEN_EXACT:
        path = _resolved(raw)
        if path is not None:
            roots.append(path)
    return roots


def personal_roots(home: Path | None = None) -> list[Path]:
    """用户个人目录（Desktop/Downloads/Documents）中实际存在的部分。"""
    base = Path.home() if home is None else Path(home)
    roots: list[Path] = []
    for name in PERSONAL_DIR_NAMES:
        path = _resolved(base / name)
        if path is not None and path.exists():
            roots.append(path)
    return roots


def protected_roots(home: Path | None = None) -> list[Path]:
    """禁止递归删除/宽通配的目录：禁区 + 个人目录。"""
    return [*forbidden_roots(), *personal_roots(home)]


def _is_protected(path: Path, home: Path | None = None) -> bool:
    """是否落在"禁止递归删除/宽通配"的区域：个人目录优先，系统禁区其次（临时目录例外）。"""
    if any(_is_under(path, root) for root in personal_roots(home)):
        return True
    temp_dir = _resolved(tempfile.gettempdir())
    if temp_dir is not None and _is_under(path, temp_dir):
        return False
    return any(_is_under(path, root) for root in forbidden_roots())


def is_forbidden(path: str | Path) -> bool:
    """路径是否属于禁区：盘符根按"精确相等"判，系统目录按"子树"判。

    例外：系统临时目录（`%TEMP%`，位于 AppData 之下）是约定的scratch 区，不算禁区。
    """
    resolved = _resolved(path)
    if resolved is None:
        return False
    temp_dir = _resolved(tempfile.gettempdir())
    if temp_dir is not None and _is_under(resolved, temp_dir):
        return False
    if any(resolved == root for root in forbidden_exact_roots()):
        return True
    return any(_is_under(resolved, root) for root in forbidden_roots())


def assert_not_forbidden(path: str | Path) -> Path:
    """写/删之前调用；禁区抛 `PathForbidden`，其余原样返回真实路径。"""
    resolved = _resolved(path) or Path(str(path))
    if is_forbidden(resolved):
        raise PathForbidden(f"路径落在禁区（系统目录），任何写入/删除都被拒绝: {resolved}")
    return resolved


def move_to_recycle_bin(path: str | Path) -> tuple[bool, str]:
    """把文件移进 Windows 回收站（不永久删除）。返回 `(成功, 错误码)`。"""
    target = _resolved(path)
    if target is None or not target.exists():
        return False, "not_found"
    if os.name != "nt":
        return False, "recycle_unavailable"
    try:
        import ctypes
        from ctypes import wintypes

        shell32 = ctypes.WinDLL("shell32", use_last_error=True)

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [
                ("hwnd", wintypes.HWND),
                ("wFunc", wintypes.UINT),
                ("pFrom", wintypes.LPCWSTR),
                ("pTo", wintypes.LPCWSTR),
                ("fFlags", ctypes.c_uint16),
                ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", ctypes.c_void_p),
                ("lpszProgressTitle", wintypes.LPCWSTR),
            ]

        FO_DELETE = 3
        FOF_SILENT = 0x0004
        FOF_NOCONFIRMATION = 0x0010
        FOF_ALLOWUNDO = 0x0040       # 进回收站，而不是永久删除
        FOF_NOERRORUI = 0x0400
        FOF_NOCONFIRMMKDIR = 0x0200

        op = SHFILEOPSTRUCTW()
        op.hwnd = None
        op.wFunc = FO_DELETE
        op.pFrom = str(target) + "\0\0"   # 双 NUL 结尾
        op.pTo = None
        op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI | FOF_NOCONFIRMMKDIR
        result = shell32.SHFileOperationW(ctypes.byref(op))
        if result != 0:
            return False, f"recycle_failed:{result}"
        if op.fAnyOperationsAborted:
            return False, "recycle_aborted"
        return True, ""
    except Exception as exc:  # noqa: BLE001 - 回收站不可用时明确拒绝，不退回永久删除
        return False, f"recycle_unavailable:{type(exc).__name__}"



@dataclass(frozen=True)
class RuleHit:
    rule: str
    action: Action
    message: str
    hint: str


# ---------------------------------------------------------------------------- 分词
_TOKEN_RE = re.compile(r'"[^"]*"|\'[^\']*\'|\S+')


def _split_top_level(text: str, *, pipes: bool) -> list[str]:
    """按引号外的分隔符切分；`pipes=True` 切单个 `|`（`||` 归语句分隔符）。"""
    parts: list[str] = []
    buf: list[str] = []
    quote = ""
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "\r":
            i += 1
            continue
        if pipes:
            if ch == "|" and not text.startswith("||", i):
                parts.append("".join(buf))
                buf = []
                i += 1
                continue
        else:
            if ch == "\n" or ch == ";":
                parts.append("".join(buf))
                buf = []
                i += 1
                continue
            if text.startswith("&&", i) or text.startswith("||", i):
                parts.append("".join(buf))
                buf = []
                i += 2
                continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _tokens(segment: str) -> list[str]:
    out: list[str] = []
    for raw in _TOKEN_RE.findall(segment):
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            raw = raw[1:-1]
        out.append(raw)
    return out


def _command_name(token: str) -> str:
    name = token.strip().strip("\"'").replace("/", "\\").rsplit("\\", 1)[-1].lower()
    for ext in (".exe", ".com", ".cmd", ".bat", ".ps1"):
        if name.endswith(ext):
            return name[: -len(ext)]
    return name


def _first_command(segment: str) -> tuple[str, list[str]]:
    tokens = _tokens(segment)
    idx = 0
    while idx < len(tokens) and tokens[idx] in {"&", "."}:
        idx += 1
    if idx >= len(tokens):
        return "", []
    return _command_name(tokens[idx]), tokens[idx + 1 :]


# ---------------------------------------------------------------------------- 路径
_VAR_HOME = ("$env:userprofile", "${env:userprofile}", "%userprofile%")


def _expand(path: str, home: Path) -> str:
    text = path.strip().strip("\"'")
    replacement = str(home)
    for var in _VAR_HOME:
        text = re.sub(re.escape(var), lambda _m: replacement, text, flags=re.IGNORECASE)
    text = re.sub(r"\$home\b", lambda _m: replacement, text, flags=re.IGNORECASE)
    text = re.sub(r"\$\{home\}", lambda _m: replacement, text, flags=re.IGNORECASE)
    if text.startswith("~"):
        text = replacement + text[1:]
    return text


def _resolve(raw: str, *, home: Path, base: Path) -> Path | None:
    text = _expand(raw, home)
    if not text:
        return None
    try:
        path = Path(text)
    except (OSError, ValueError):
        return None
    if not path.is_absolute():
        path = base / path
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        return None


def _is_root_like(path: Path, *, home: Path) -> bool:
    """盘符根 / 文件系统根 / 用户主目录本身。"""
    text = str(path)
    if text in {"/", "\\"}:
        return True
    try:
        if path == Path(path.anchor):
            return True
    except (OSError, ValueError):
        pass
    try:
        return path == home
    except (OSError, ValueError):
        return False


def _is_under(child: Path, root: Path) -> bool:
    try:
        child.relative_to(root)
        return True
    except (ValueError, OSError):
        return False


# ---------------------------------------------------------------------------- 规则常量
_R1_NAMES = {"format", "format-volume", "diskpart", "clear-disk"}
_R2_REMOVE = {
    "rm",
    "remove-item",
    "ri",
    "del",
    "erase",
    "rd",
    "rmdir",
    "deltree",
}
_R2_UNIX_STYLE = {"rm", "rmdir"}
_R3_NAMES = {"shutdown", "restart-computer", "stop-computer", "logoff"}
_R5_NAMES = {"runas"}
_R6_DOWNLOAD = {
    "curl",
    "wget",
    "iwr",
    "invoke-webrequest",
    "invoke-restmethod",
    "certutil",
    "bitsadmin",
}
_R6_EVAL = {
    "iex",
    "invoke-expression",
    "sh",
    "bash",
    "zsh",
    "powershell",
    "pwsh",
    "cmd",
    "python",
    "python3",
    "node",
    "perl",
}
_R7_READ = {"cat", "type", "get-content", "gc"}
_R7_SEND = {
    "curl",
    "wget",
    "iwr",
    "invoke-webrequest",
    "invoke-restmethod",
    "scp",
    "sftp",
    "ftp",
    "nc",
    "ssh",
    "send-mailmessage",
}
_R8_WRITE = {
    "set-content",
    "out-file",
    "add-content",
    "new-item",
    "copy-item",
    "move-item",
    "rename-item",
    "mkdir",
    "md",
    "touch",
    "tee",
    "cp",
    "mv",
    "rm",
    "remove-item",
    "del",
    "erase",
    "rd",
    "rmdir",
    "ri",
}

_R7_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", re.IGNORECASE)
_R7_AUTH_HEADER = re.compile(r"\bauthorization\s*:", re.IGNORECASE)
_R7_SENSITIVE_FILE = re.compile(r"(^|[\\/])\.env($|\.)|(^|[\\/])id_(rsa|ed25519|dsa)($|\.)", re.IGNORECASE)
_REDIRECT = re.compile(r"(?<![0-9&])>>?\s*(\"[^\"]+\"|'[^']+'|[^\s;|&]+)")


def _is_flag(token: str, command: str) -> bool:
    if token.startswith("-"):
        return True
    if command in _R2_UNIX_STYLE:
        return False
    # cmdlet / cmd 风格：`/s`、`/q` 是开关；`/` 与 `C:\` 是路径
    return len(token) > 1 and token.startswith("/") and not re.match(r"^/[^\\/]+[\\/]", token)


def _recursive(flags: list[str], command: str) -> bool:
    for flag in flags:
        low = flag.lower()
        if low in {"-r", "-rf", "-fr", "--recursive", "-recurse", "/s"}:
            return True
        if command in _R2_UNIX_STYLE and re.fullmatch(r"-[a-z]*r[a-z]*", low):
            return True
    return False


# ---------------------------------------------------------------------------- 各规则
def _hit_r1(command: str, args: list[str]) -> RuleHit | None:
    if command in _R1_NAMES:
        return RuleHit(
            "R1",
            "deny",
            f"该命令会对磁盘/分区做不可逆操作（{command}），已被策略拒绝。",
            "磁盘级操作不在助手的能力范围内。如需扩容/清理，请手动在系统设置里处理。",
        )
    if command == "cipher" and any(arg.lower() in {"/w", "/w:"} or arg.lower().startswith("/w:") for arg in args):
        return RuleHit(
            "R1",
            "deny",
            "cipher /w 会擦除磁盘空闲空间，已被策略拒绝。",
            "该操作不可逆且影响整块磁盘；请手动执行。",
        )
    return None


def _hit_r2(
    command: str,
    args: list[str],
    *,
    home: Path,
    base: Path,
) -> RuleHit | None:
    if command not in _R2_REMOVE:
        return None
    flags = [a for a in args if _is_flag(a, command)]
    recursive = _recursive(flags, command)
    targets = [a for a in args if not _is_flag(a, command)]
    for raw in targets:
        path = _resolve(raw, home=home, base=base)
        if path is None:
            continue
        # 个人目录 / 系统禁区上的宽通配（如 `*.tmp`、`*.log`）：不区分是否递归，一律拒绝。
        if re.search(r"[*?]", raw) and _is_protected(path, home):
            return RuleHit(
                "R2",
                "deny",
                "该命令在个人目录/系统目录上使用宽通配，已被策略拒绝。",
                "请列出具体文件（可用 Glob 先查看），再逐个处理。",
            )
        if not recursive:
            continue
        if _is_root_like(path, home=home):
            return RuleHit(
                "R2",
                "deny",
                "该命令会递归删除用户目录或盘符根，已被策略拒绝。",
                "如确需清理，请指定具体子目录（例如某项目的 build 目录），不要删除根目录。",
            )
        if _is_protected(path, home):
            return RuleHit(
                "R2",
                "deny",
                "该命令会递归删除个人目录或系统目录，已被策略拒绝。",
                "个人文件请指明具体文件并走回收站；系统目录不在助手的能力范围内。",
            )
    return None


def _hit_r5(command: str, args: list[str]) -> RuleHit | None:
    if command in _R5_NAMES:
        return RuleHit(
            "R5",
            "deny",
            "请求提权（runas / RunAs）已被策略拒绝。",
            "助手不自我提权。若确实需要管理员权限，请由用户手动执行。",
        )
    if command == "start-process":
        lowered = [a.lower() for a in args]
        for idx, arg in enumerate(lowered):
            if arg in {"-verb", "/verb"} and idx + 1 < len(lowered) and lowered[idx + 1] == "runas":
                return RuleHit(
                    "R5",
                    "deny",
                    "请求提权（Start-Process -Verb RunAs）已被策略拒绝。",
                    "助手不自我提权。若确实需要管理员权限，请由用户手动执行。",
                )
    return None


def _hit_r6(command: str, args: list[str], statements: list[list[tuple[str, list[str]]]]) -> RuleHit | None:
    if command in _R6_EVAL:
        joined = " ".join(args)
        if "<(" in joined and re.search(r"curl|wget|https?://", joined, re.IGNORECASE):
            return RuleHit(
                "R6",
                "deny",
                "从网络直接执行脚本（`<(...)` 管道）已被策略拒绝。",
                "远程脚本先下载到工作区、Read 审查内容后再手动执行。",
            )
    for segments in statements:
        names = [name for name, _ in segments]
        if len(segments) < 2:
            continue
        for idx, name in enumerate(names[:-1]):
            if name in _R6_DOWNLOAD and any(later in _R6_EVAL for later in names[idx + 1 :]):
                return RuleHit(
                    "R6",
                    "deny",
                    "从网络下载并直接执行（下载 | 解释器）已被策略拒绝。",
                    "先下载到工作区并 Read 检查，确认安全后再执行。",
                )
    return None


def _hit_r7(command: str, statements: list[list[tuple[str, list[str]]]]) -> RuleHit | None:
    if _R7_PRIVATE_KEY.search(command):
        return RuleHit(
            "R7",
            "deny",
            "命令里出现私钥内容，已被策略拒绝。",
            "私钥不应进入命令行或日志；请改用已配置好的凭据存储。",
        )
    if _R7_AUTH_HEADER.search(command):
        return RuleHit(
            "R7",
            "deny",
            "命令里出现 Authorization 头，已被策略拒绝。",
            "凭据不要写进命令行；请使用环境变量或凭据文件并通过工具读取。",
        )
    all_names = {name for segments in statements for name, _ in segments}
    if not (all_names & _R7_SEND):
        return None
    for segments in statements:
        for name, args in segments:
            if name not in _R7_READ:
                continue
            if any(_R7_SENSITIVE_FILE.search(arg) for arg in args):
                return RuleHit(
                    "R7",
                    "deny",
                    "读取 .env / 私钥并外发已被策略拒绝。",
                    "凭据内容不外发。若需要调试，请只读取非敏感字段。",
                )
    return None


def _hit_r3(command: str) -> RuleHit | None:
    if command in _R3_NAMES:
        return RuleHit(
            "R3",
            "need_confirm",
            "该命令会关机/重启/注销当前机器，需要用户确认。",
            "先向用户说明影响并取得明确同意，再带 confirmed=true 重新调用。",
        )
    return None


def _hit_r4(command: str, args: list[str]) -> RuleHit | None:
    lowered = [a.lower() for a in args]
    hit = (
        (command == "reg" and bool(lowered) and lowered[0] in {"add", "delete"})
        or command in {"set-service", "new-service"}
        or (command == "schtasks" and "/create" in lowered)
    )
    if not hit:
        return None
    return RuleHit(
        "R4",
        "need_confirm",
        "该命令会修改注册表/服务/计划任务，需要用户确认。",
        "先向用户说明会改什么并取得明确同意，再带 confirmed=true 重新调用。",
    )


def _looks_like_path(token: str) -> bool:
    if not token or token.startswith("-"):
        return False
    if "://" in token:
        return False
    low = token.lower()
    if token in {"*", "/", "\\"}:
        return True
    if token.startswith("~") or low.startswith("$home") or low.startswith("$env:userprofile"):
        return True
    if low.startswith("%userprofile%") or low.startswith("%systemdrive%"):
        return True
    if re.match(r"^[a-zA-Z]:[\\/]", token):
        return True
    return "\\" in token or "/" in token


def _hit_r8(
    command: str,
    args: list[str],
    *,
    workspace_root: Path,
    base: Path,
    home: Path,
    extra_targets: list[str],
    write_roots: list[Path] | None = None,
) -> RuleHit | None:
    if command not in _R8_WRITE and not extra_targets:
        return None
    roots = write_roots or [workspace_root]
    targets = [a for a in args if not _is_flag(a, command) and _looks_like_path(a)]
    targets.extend(extra_targets)
    for raw in targets:
        path = _resolve(raw, home=home, base=base)
        if path is None or _is_root_like(path, home=home):
            continue  # 根目录交给 R2/R1，不重复判
        if any(_is_under(path, root) for root in roots):
            continue
        return RuleHit(
            "R8",
            "need_confirm",
            "该命令会在可玩耍区之外写入或删除，需要用户确认。",
            "先向用户说明目标路径并取得明确同意，再带 confirmed=true 重新调用。",
        )
    return None


def _redirect_targets(statement: str) -> list[str]:
    out: list[str] = []
    for match in _REDIRECT.finditer(statement):
        target = match.group(1).strip().strip("\"'")
        if target and not target.startswith("&"):
            out.append(target)
    return out


def check_command_rules(
    command: str,
    *,
    workspace_root: Path,
    base_dir: Path | None = None,
    home: Path | None = None,
    write_roots: list[Path] | None = None,
) -> RuleHit | None:
    """按 R1–R8 对一条命令做确定性判断；无命中返回 None。

    拒绝类（R1/R2/R5/R6/R7）优先于确认类（R3/R4/R8）：同一条命令里既有
    危险动作又有待确认动作时，直接拒绝，避免"部分执行"。
    `write_roots` 是可玩耍区（默认 `[workspace_root]`），R8 以它为界判越界写。
    """
    if not command or not command.strip():
        return None

    home_dir = Path.home() if home is None else Path(home)
    base = Path(base_dir) if base_dir is not None else Path(workspace_root)
    try:
        workspace = Path(workspace_root).resolve(strict=False)
    except (OSError, RuntimeError):
        workspace = Path(workspace_root)
    roots: list[Path] = []
    for raw in (write_roots or [workspace]):
        resolved = _resolved(raw)
        if resolved is not None and resolved not in roots:
            roots.append(resolved)
    statements: list[tuple[str, list[tuple[str, list[str]]]]] = []
    for statement in _split_top_level(command, pipes=False):
        segments = [
            (name, args)
            for name, args in (_first_command(s) for s in _split_top_level(statement, pipes=True))
            if name
        ]
        if segments:
            statements.append((statement, segments))

    deny_hits: list[RuleHit] = []
    confirm_hits: list[RuleHit] = []

    for statement, segments in statements:
        redirects = _redirect_targets(statement)
        for name, args in segments:
            r1 = _hit_r1(name, args)
            if r1:
                deny_hits.append(r1)
            r2 = _hit_r2(name, args, home=home_dir, base=base)
            if r2:
                deny_hits.append(r2)
            r5 = _hit_r5(name, args)
            if r5:
                deny_hits.append(r5)
            r6_sub = _hit_r6(name, args, [])
            if r6_sub:
                deny_hits.append(r6_sub)
            r3 = _hit_r3(name)
            if r3:
                confirm_hits.append(r3)
            r4 = _hit_r4(name, args)
            if r4:
                confirm_hits.append(r4)
            r8 = _hit_r8(
                name,
                args,
                workspace_root=workspace,
                base=base,
                home=home_dir,
                extra_targets=redirects,
                write_roots=roots,
            )
            if r8:
                confirm_hits.append(r8)
        r6 = _hit_r6("", [], [segments]) if len(segments) > 1 else None
        if r6:
            deny_hits.append(r6)
        r7 = _hit_r7(command, [segments])
        if r7:
            deny_hits.append(r7)

    if deny_hits:
        return deny_hits[0]
    if confirm_hits:
        return confirm_hits[0]
    return None
