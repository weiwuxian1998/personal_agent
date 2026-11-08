"""Shell 错误归一化：输出清洗、错误分类、固定 hint 与错误指纹。

设计依据：契约 §7.2 第 6 条、设计文档 §7.5.1/§7.5.2。
本模块只依赖标准库；`RetryTracker` 只做进程内可观测计数，真正的熔断预算
由 M0 的 `ShellGuardMiddleware`（Redis）实现，这里不拦截执行。
"""

from __future__ import annotations

import hashlib
import html
import re
import threading
from dataclasses import dataclass

# 契约 §7.2 规定的 8 类 error_class（顺序即分类优先级）。
ERROR_CLASSES: tuple[str, ...] = (
    "parse_error",
    "command_not_found",
    "permission",
    "encoding",
    "timeout",
    "network",
    "not_found_path",
    "exit_nonzero",
)

# ----------------------------------------------------------------------------- 模式表
_PARSE_MARKERS = (
    "not a valid statement separator",
    "unexpected token",
    "missing closing",
    "missing the terminator",
    "parsererror",
    "不是此版本中的有效语句分隔符",
    "意外的标记",
    "缺少语句结束",
    "缺少右",
    "表达式或语句中包含意外的",
)

_COMMAND_NOT_FOUND_MARKERS = (
    "is not recognized as the name of a cmdlet",
    "is not recognized as a name of a cmdlet",
    "not recognized as the name of a cmdlet",
    "not recognized as a name of a cmdlet",
    "is not recognized as an internal or external command",
    "command not found",
    "commandnotfoundexception",
    "不是内部或外部命令",
    "识别为 cmdlet",
    "找不到命令",
)

_PERMISSION_MARKERS = (
    "access is denied",
    "permission denied",
    "requires elevation",
    "unauthorizedaccessexception",
    "accessdenied",
    "拒绝访问",
    "权限不足",
    "权限不够",
    "没有足够权限",
)

_ENCODING_MARKERS = (
    "unicodeencodeerror",
    "unicodedecodeerror",
    "codec can't decode",
    "codec can't encode",
    "can't decode byte",
    "can't encode",
    "illegal multibyte sequence",
    "编码错误",
)

_NETWORK_MARKERS = (
    "name or service not known",
    "temporary failure in name resolution",
    "connection refused",
    "connection reset",
    "connection timed out",
    "tls handshake",
    "proxyerror",
    "getaddrinfo failed",
    "network is unreachable",
    "无法解析主机",
    "连接被拒绝",
    "远程主机强迫关闭",
)

_NOT_FOUND_PATH_MARKERS = (
    "cannot find path",
    "no such file or directory",
    "could not find a part of the path",
    "could not find file",
    "itemnotfoundexception",
    "系统找不到指定的路径",
    "找不到路径",
    "无法找到路径",
    "路径不存在",
    "because it does not exist",
)

_REPLACEMENT_RATIO = 0.05
_REPLACEMENT_MIN_CHARS = 20

# ----------------------------------------------------------------------------- 固定 hint
HINT_PARSE = "检查引号/括号是否配对；PowerShell 5.1 不支持 &&，改用 ';' 分隔语句"
HINT_AMP_PS5 = (
    "PowerShell 5.1 不支持 && / ||：改用 ';' 分隔语句；"
    "需要判断成功与否用 if ($LASTEXITCODE -ne 0) { ... }"
)
HINT_PY_C = (
    "python -c 的内嵌引号容易在多层转义中丢失："
    "把多行代码写到 .py 文件，再执行 python <文件>"
)
HINT_COMMAND_NOT_FOUND = (
    "先用 Get-Command <name> 确认命令是否存在；"
    "没有就改用绝对路径，或说明需要先安装"
)
HINT_PERMISSION = "不要尝试提权；把目标改到工作区内有权限的路径，或改用专用文件工具"
HINT_ENCODING = (
    "显式设置 [Console]::OutputEncoding=[Text.Encoding]::UTF8 与 $OutputEncoding；"
    "给原生程序管道输入时尤其要设"
)
HINT_TIMEOUT = (
    "拆成更小的步骤，或提高 timeout_ms（worker 上限 600000）；"
    "长任务请在后台启动并记录 pid"
)
HINT_NETWORK = "检查代理/网络连通性；抓取网页优先走 WebFetch 端点"
HINT_NOT_FOUND_PATH = "先用 Test-Path 或 Glob 确认路径存在；路径写绝对路径"
HINT_EXIT_NONZERO = "命令执行了但返回非零；查看 stderr 末尾定位原因"

_HINT_BY_CLASS = {
    "parse_error": HINT_PARSE,
    "command_not_found": HINT_COMMAND_NOT_FOUND,
    "permission": HINT_PERMISSION,
    "encoding": HINT_ENCODING,
    "timeout": HINT_TIMEOUT,
    "network": HINT_NETWORK,
    "not_found_path": HINT_NOT_FOUND_PATH,
    "exit_nonzero": HINT_EXIT_NONZERO,
}

_RETRYABLE = frozenset({"timeout", "network", "exit_nonzero"})


@dataclass(frozen=True, slots=True)
class ErrorInfo:
    """一次失败归一化的结果。`hint_key` 参与指纹，便于区分同类不同建议。"""

    error_class: str
    hint: str
    hint_key: str
    retryable: bool


# ----------------------------------------------------------------------------- 输出清洗
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_CLIXML_START = "#< CLIXML"
_CLIXML_END = "</Objs>"
_CLIXML_ERROR_RE = re.compile(r'<S\s+S="Error">(.*?)</S>', re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


def strip_clixml(text: str) -> str:
    """剥离 PowerShell 非交互场景混入 stderr 的 `#< CLIXML ... </Objs>` 块。

    CLIXML 里的错误文本是 XML 转义过的，`_x000D__x000A_` 等需要还原成换行。
    """
    if not text or _CLIXML_START not in text:
        return text
    start = text.find(_CLIXML_START)
    end = text.find(_CLIXML_END, start)
    block = text[start:] if end < 0 else text[start : end + len(_CLIXML_END)]
    body = "".join(_CLIXML_ERROR_RE.findall(block)) or _TAG_RE.sub("", block)
    body = html.unescape(body)
    body = (
        body.replace("_x000D__x000A_", "\n")
        .replace("_x000D_", "\r")
        .replace("_x000A_", "\n")
    )
    rest = text[:start] + body + (text[end + len(_CLIXML_END) :] if end >= 0 else "")
    return rest.strip("\r\n")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


def clean_text(text: str) -> str:
    """顺序固定：CLIXML → ANSI → 统一换行。"""
    text = strip_clixml(text or "")
    text = strip_ansi(text)
    return text.replace("\r\n", "\n").replace("\r", "\n")


# ----------------------------------------------------------------------------- 分类
def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    low = text.lower()
    return any(marker.lower() in low for marker in markers)


def _replacement_ratio(text: str) -> float:
    if len(text) < _REPLACEMENT_MIN_CHARS:
        return 0.0
    return text.count("\ufffd") / len(text)


def _looks_like_python_c(command: str) -> bool:
    return bool(
        re.search(r"(?:^|[\s&;|])(?:python|py)(?:\.exe)?\s+-c(?:\s|$)", command, re.IGNORECASE)
    )


def classify_error(
    exit_code: int | None,
    text: str,
    *,
    timed_out: bool = False,
    command: str = "",
    shell_name: str = "",
) -> ErrorInfo | None:
    """按契约 §7.5.1 的优先级归一化；返回 None 表示成功。"""
    if timed_out:
        return ErrorInfo("timeout", HINT_TIMEOUT, "timeout", True)

    if exit_code == 0:
        if _replacement_ratio(text) > _REPLACEMENT_RATIO:
            return ErrorInfo("encoding", HINT_ENCODING, "encoding", False)
        return None

    if _contains_any(text, _PARSE_MARKERS):
        if shell_name == "powershell" and "&&" in command:
            return ErrorInfo("parse_error", HINT_AMP_PS5, "amp_ps5", False)
        if _looks_like_python_c(command):
            return ErrorInfo("parse_error", HINT_PY_C, "python_c_quotes", False)
        return ErrorInfo("parse_error", HINT_PARSE, "parse_error", False)

    if _contains_any(text, _COMMAND_NOT_FOUND_MARKERS):
        return ErrorInfo("command_not_found", HINT_COMMAND_NOT_FOUND, "command_not_found", False)

    if _contains_any(text, _PERMISSION_MARKERS):
        return ErrorInfo("permission", HINT_PERMISSION, "permission", False)

    if _contains_any(text, _ENCODING_MARKERS) or _replacement_ratio(text) > _REPLACEMENT_RATIO:
        return ErrorInfo("encoding", HINT_ENCODING, "encoding", False)

    if _contains_any(text, _NETWORK_MARKERS):
        return ErrorInfo("network", HINT_NETWORK, "network", True)

    if _contains_any(text, _NOT_FOUND_PATH_MARKERS):
        return ErrorInfo("not_found_path", HINT_NOT_FOUND_PATH, "not_found_path", False)

    return ErrorInfo("exit_nonzero", HINT_EXIT_NONZERO, "exit_nonzero", True)


# ----------------------------------------------------------------------------- 指纹
_QUOTED_RE = re.compile(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'')
_WIN_PATH_RE = re.compile(r'(?<![\w])(?:[A-Za-z]:[\\/]|\\\\)[^\s"\'|;<>]*')


def normalize_command_for_fingerprint(command: str) -> str:
    """命令骨架化：小写 + 折叠空白 + 引号字面量→<lit> + 绝对路径→<path>。"""
    text = (command or "").lower()
    text = _QUOTED_RE.sub("<lit>", text)
    text = _WIN_PATH_RE.sub("<path>", text)
    return " ".join(text.split())


def fingerprint(shell_name: str, command: str, error_class: str, hint_key: str) -> str:
    """sha256(shell|规范化命令|error_class|hint_key)[:12]。"""
    raw = "|".join(
        (
            shell_name or "",
            normalize_command_for_fingerprint(command),
            error_class or "",
            hint_key or "",
        )
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


# ----------------------------------------------------------------------------- 摘要
_FIXED_MESSAGE = {
    "parse_error": "命令语法解析失败",
    "command_not_found": "命令或程序未找到",
    "permission": "权限不足",
    "encoding": "输出编码异常（解码后出现大量替换字符）",
    "network": "网络请求失败",
    "not_found_path": "路径不存在",
    "exit_nonzero": "命令返回非零退出码",
}


def _first_error_line(text: str, limit: int = 300) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line and line != "--- stderr ---":
            return line if len(line) <= limit else line[:limit] + "…"
    return ""


def summarize_error(
    info: ErrorInfo,
    text: str,
    *,
    exit_code: int | None = None,
    timeout_ms: int | None = None,
) -> str:
    """给模型看的短消息：固定措辞 + 首行错误片段（不复制整段输出）。"""
    if info.error_class == "timeout":
        msg = (
            f"命令超过 {timeout_ms} ms 未结束，已终止整棵进程树"
            if timeout_ms
            else "命令超时，已终止整棵进程树"
        )
    elif info.error_class == "exit_nonzero":
        msg = f"命令返回非零退出码 {exit_code}"
    elif info.error_class == "parse_error" and info.hint_key == "amp_ps5":
        msg = "本机 PowerShell 5.1 不支持 '&&' 语句分隔符"
    elif info.error_class == "parse_error" and info.hint_key == "python_c_quotes":
        msg = "python -c 的内嵌引号在转义过程中丢失"
    else:
        msg = _FIXED_MESSAGE.get(info.error_class, "命令执行失败")
    line = _first_error_line(text)
    if line and line not in msg:
        msg = f"{msg}：{line}"
    return msg


# ----------------------------------------------------------------------------- 进程内计数
class RetryTracker:
    """记录同一指纹/同一命令骨架的连续失败次数（只观测，不拦截）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._fp: dict[str, int] = {}
        self._cmd: dict[str, int] = {}
        self._last_fp: dict[str, str] = {}

    def record_failure(self, command_key: str, fp: str) -> tuple[int, int]:
        """返回 (same_fp_count, attempt)。"""
        with self._lock:
            if len(self._fp) > 4096:
                self._fp.clear()
            if len(self._cmd) > 4096:
                self._cmd.clear()
            self._cmd[command_key] = self._cmd.get(command_key, 0) + 1
            self._fp[fp] = self._fp.get(fp, 0) + 1
            self._last_fp[command_key] = fp
            return self._fp[fp], self._cmd[command_key]

    def record_success(self, command_key: str) -> None:
        with self._lock:
            self._cmd.pop(command_key, None)
            fp = self._last_fp.pop(command_key, None)
            if fp:
                self._fp.pop(fp, None)

    def reset(self) -> None:
        with self._lock:
            self._fp.clear()
            self._cmd.clear()
            self._last_fp.clear()
