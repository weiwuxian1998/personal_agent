"""M-WEB Web 工具：WebSearch / WebFetch（契约 §7.7）。

设计约束（照契约执行）：
- 独立薄封装，**不复用** OpenAI chat 客户端：两个端点都是自有 JSON 协议；
- 凭证只读 `settings.deepseek_search_api_key`；未配置返回结构化 `not_configured`，
  **禁止**回退读 LLM 凭证（代码层面不出现该字段名）；
- `401 auth_error` / `402 insufficient_balance` 单独归类且**禁止重试**；
  只有 408 / 429 / 5xx / 网络异常最多重试 1 次；
- `content` 超过公共阈值（`tool_output_max_lines` / `tool_output_max_bytes`）时落盘，
  复用 `tools/shell.py` 的 `needs_artifact()` / `stub_text()`，不另写一套 stub；
- 工具无状态：每次调用新建 httpx 客户端；不缓存搜索结果。
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from langchain_core.tools import BaseTool, tool

from agents.context import current_tool_call_id, current_trace_id
from config import Settings
from contracts import tool_error, ulid
from tools.shell import needs_artifact, stub_text

# ---------------------------------------------------------------- 端点与限额
_SEARCH_PATH = "/v1/tools/search"
_FETCH_PATH = "/v1/tools/fetch"
_DEFAULT_BASE_URL = "https://api.deepseek.com"

_MIN_COUNT = 1
_MAX_COUNT = 50
_MAX_QUERY_CHARS = 128
_MAX_URL_CHARS = 8192
_MAX_SERVER_ERROR_CHARS = 300

# 契约 §7.7：只有这几类允许重试，且总计 ≤1 次
_MAX_RETRIES = 1
_RETRY_SLEEP_S = 0.5
_MAX_RETRY_AFTER_S = 3.0
_RETRYABLE = frozenset({"timeout", "rate_limited", "upstream_error", "network_error"})

_STUB_HINT = (
    "正文已落盘：用 Read 的 offset/limit 分页读取，或对落盘文件用 Grep 定位关键段落；"
    "title/url/snippet 体积小且是定位信息，不落盘。"
)

_FIXED_MESSAGE = {
    "invalid_args": "请求参数不合法",
    "auth_error": "搜索凭证无效（401）",
    "insufficient_balance": "搜索账户余额不足（402）",
    "forbidden": "服务端安全策略拒绝（403）",
    "not_found": "抓不到页面正文（404）",
    "timeout": "请求超时",
    "rate_limited": "请求频率超限（429）",
    "upstream_error": "上游服务异常（5xx）",
    "network_error": "网络请求失败",
    "http_error": "HTTP 请求失败",
}

_HINT = {
    "not_configured": (
        "在 .env 中填写 DEEPSEEK_SEARCH_API_KEY（只用于 /v1/tools/search 与 "
        "/v1/tools/fetch）；禁止用 LLM_API_KEY 代替，代码不支持互 fallback"
    ),
    "invalid_args": (
        "query 1–128 字符、count 1–50、url 必须是带 host 的 http/https（≤8192，不含账号密码）；"
        "本地校验失败不会发请求"
    ),
    "auth_error": "DEEPSEEK_SEARCH_API_KEY 无效或已轮换：更新 .env 后重启；已禁止自动重试",
    "insufficient_balance": "搜索账户余额不足：充值后再调用；重试只会继续扣预算，已禁止自动重试",
    "forbidden": "服务端安全策略拒绝（通常是查询/来源内容问题），重试无用",
    "not_found": "该 URL 抓不到正文（需登录/反爬）；改用 WebSearch 返回的 summary",
    "timeout": "端点超时：已自动重试 1 次；仍失败请缩短查询或稍后再试",
    "rate_limited": "QPS 超限（search 100/s、fetch 25/s）：已退避重试 1 次；请放慢调用频率",
    "upstream_error": "上游 5xx：已重试 1 次；仍失败请稍后再试",
    "network_error": "网络异常：检查代理/连通性；已重试 1 次",
    "http_error": "端点返回了契约未列出的状态码；按返回体排查，不要盲目重试",
}


# ---------------------------------------------------------------- HTTP 薄层
@dataclass(frozen=True, slots=True)
class _HttpResult:
    """一次 HTTP 尝试的结果；`status is None` 表示传输层失败。"""

    status: int | None
    body: str = ""
    kind: str = ""          # "timeout" | "network"
    detail: str = ""
    retry_after: float | None = None


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value.strip())
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


def _post_json(
    url: str,
    payload: dict[str, Any],
    *,
    api_key: str,
    timeout_s: float,
) -> _HttpResult:
    """发一次 JSON POST；不重试、不解析业务语义（便于验收脚本桩替换）。"""
    total = max(1.0, float(timeout_s))
    timeout = httpx.Timeout(total, connect=min(10.0, total))
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, json=payload, headers=headers)
    except httpx.TimeoutException as exc:
        return _HttpResult(None, kind="timeout", detail=f"请求超时: {exc}")
    except httpx.HTTPError as exc:
        return _HttpResult(None, kind="network", detail=f"网络异常: {exc}")
    return _HttpResult(
        resp.status_code,
        resp.text,
        retry_after=_parse_retry_after(resp.headers.get("retry-after")),
    )


def _classify_status(status: int) -> str:
    if status == 400:
        return "invalid_args"
    if status == 401:
        return "auth_error"
    if status == 402:
        return "insufficient_balance"
    if status == 403:
        return "forbidden"
    if status == 404:
        return "not_found"
    if status == 408:
        return "timeout"
    if status == 413:
        return "invalid_args"
    if status == 429:
        return "rate_limited"
    if 500 <= status <= 599:
        return "upstream_error"
    return "http_error"


def _server_error_text(body: str) -> str:
    """抽取服务端 `error` 原文（最多 300 字符），解析不了就退回原始 body。"""
    text = (body or "").strip()
    if not text:
        return ""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text[:_MAX_SERVER_ERROR_CHARS]
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            value = err.get("message") or err.get("msg") or json.dumps(err, ensure_ascii=False)
        elif isinstance(err, str):
            value = err
        else:
            value = data.get("message") or ""
        if value:
            text = str(value)
    return " ".join(text.split())[:_MAX_SERVER_ERROR_CHARS]


def _failure(
    error_class: str,
    *,
    status: int | None = None,
    detail: str = "",
    attempts: int = 1,
) -> dict[str, Any]:
    message = _FIXED_MESSAGE.get(error_class, "请求失败")
    extra_text = _server_error_text(detail)
    if extra_text and extra_text not in message:
        message = f"{message}：{extra_text}"
    extra: dict[str, Any] = {
        "retryable": error_class in _RETRYABLE,
        "attempts": attempts,
    }
    if status is not None:
        extra["status_code"] = status
    return tool_error(error_class, message, hint=_HINT.get(error_class, ""), **extra)


def _shape_failure(message: str, *, status: int = 200, attempts: int = 1) -> dict[str, Any]:
    """2xx 但返回体不符合契约结构：不可重试，避免把格式问题当故障刷额度。"""
    return tool_error(
        "upstream_error",
        message,
        hint="上游返回体不符合契约结构；先核对该端点/版本，不要盲目重试",
        retryable=False,
        attempts=attempts,
        status_code=status,
    )


def _call_json(
    url: str,
    payload: dict[str, Any],
    *,
    api_key: str,
    timeout_s: float,
) -> dict[str, Any]:
    """带一次重试的调用；成功返回 `{"ok": True, "data": {...}}`。"""
    attempts = 0
    while True:
        attempts += 1
        result = _post_json(url, payload, api_key=api_key, timeout_s=timeout_s)

        if result.status is None:
            error_class = "timeout" if result.kind == "timeout" else "network_error"
            if error_class in _RETRYABLE and attempts <= _MAX_RETRIES:
                time.sleep(_RETRY_SLEEP_S)
                continue
            return _failure(error_class, detail=result.detail, attempts=attempts)

        if 200 <= result.status < 300:
            try:
                data = json.loads(result.body or "{}")
            except json.JSONDecodeError:
                return _shape_failure("返回体不是合法 JSON", status=result.status, attempts=attempts)
            if not isinstance(data, dict):
                return _shape_failure("返回体不是 JSON 对象", status=result.status, attempts=attempts)
            return {"ok": True, "data": data}

        error_class = _classify_status(result.status)
        if error_class in _RETRYABLE and attempts <= _MAX_RETRIES:
            delay = _RETRY_SLEEP_S
            if error_class == "rate_limited" and result.retry_after is not None:
                delay = min(result.retry_after, _MAX_RETRY_AFTER_S)
            time.sleep(max(0.0, delay))
            continue
        return _failure(
            error_class,
            status=result.status,
            detail=result.body,
            attempts=attempts,
        )


# ---------------------------------------------------------------- 本地校验
def _local_failure(message: str) -> dict[str, Any]:
    """本地参数校验失败：不发请求，attempts=0。"""
    return tool_error(
        "invalid_args",
        message,
        hint=_HINT["invalid_args"],
        retryable=False,
        attempts=0,
    )


def _validate_query(query: Any) -> dict[str, Any] | None:
    if not isinstance(query, str) or not query.strip():
        return _local_failure("query 不能为空")
    if len(query) > _MAX_QUERY_CHARS:
        return _local_failure(f"query 长度 {len(query)} 超过上限 {_MAX_QUERY_CHARS}")
    return None


def _validate_url(url: Any) -> dict[str, Any] | None:
    if not isinstance(url, str) or not url:
        return _local_failure("url 不能为空")
    if len(url) > _MAX_URL_CHARS:
        return _local_failure(f"url 长度 {len(url)} 超过上限 {_MAX_URL_CHARS}")
    if any(ch.isspace() for ch in url):
        return _local_failure("url 不能包含空白字符")
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError as exc:
        return _local_failure(f"url 无法解析: {exc}")
    if parts.scheme.lower() not in ("http", "https"):
        return _local_failure("url 必须是 http/https 协议")
    if not parts.hostname:
        return _local_failure("url 必须带 host")
    if parts.username or parts.password:
        return _local_failure("url 不能包含账号密码")
    return None


# ---------------------------------------------------------------- 凭证与脱敏
def mask_api_key(key: str) -> str:
    """脱敏：只保留前 5 位与后 4 位（契约 §7.7 凭证隔离）。"""
    if not key:
        return "<empty>"
    if len(key) <= 9:
        return "***"
    return f"{key[:5]}...{key[-4:]}"


# ---------------------------------------------------------------- 落盘 stub
def _safe_part(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z_.-]+", "_", value or "")[:64].strip("._")
    return cleaned or fallback


def _artifact_path(outputs_dir: Path, trace_id: str, call_id: str, index: int) -> Path:
    """契约 §7.7：`tool_outputs/<trace_id>/<tool_call_id>-<n>.md`。"""
    trace = _safe_part(trace_id, "notrace")
    call = _safe_part(call_id, ulid())
    return (Path(outputs_dir) / trace / f"{call}-{index}.md").resolve()


def _maybe_stub(
    content: str,
    *,
    header: str,
    index: int,
    outputs_dir: Path,
    trace_id: str,
    call_id: str,
    max_lines: int,
    max_bytes: int,
    head_lines: int,
    tail_lines: int,
) -> tuple[str, str | None]:
    """超阈值时落盘并返回 (stub 文本, artifact 绝对路径)；否则原样返回。"""
    if not content or not needs_artifact(content, max_lines=max_lines, max_bytes=max_bytes):
        return content, None
    target = _artifact_path(outputs_dir, trace_id, call_id, index)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8", newline="\n")
    text = stub_text(
        content,
        str(target),
        head_lines=head_lines,
        tail_lines=tail_lines,
        header=header,
        hint=_STUB_HINT,
    )
    return text, str(target)


# ---------------------------------------------------------------- 字段归一化
def _as_str(value: Any) -> str:
    return value if isinstance(value, str) else ("" if value is None else str(value))


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _normalize_result(item: Any) -> dict[str, Any]:
    """按契约 §7.7 的实测字段补默认值；不臆造、不改名。"""
    data = item if isinstance(item, dict) else {}
    return {
        "title": _as_str(data.get("title")),
        "url": _as_str(data.get("url")),
        "snippet": _as_str(data.get("snippet")),
        "summary": _as_str(data.get("summary")),
        "content": _as_str(data.get("content")),
        "site_name": _as_str(data.get("site_name")),
        "site_icon": _as_str(data.get("site_icon")),
        "authority": _as_int(data.get("authority")),
        "rerank_score": _as_float(data.get("rerank_score")),
        "date": _as_str(data.get("date")),
    }


# ---------------------------------------------------------------- 工厂
def build_web_tools(*, settings: Settings, tool_outputs_dir: Path) -> list[BaseTool]:
    """返回 [WebSearch, WebFetch]（契约 §7.7 冻结签名）。

    参数：
    - settings: 全局配置；凭证只取 `deepseek_search_api_key`，端点取 `search_api_base_url`；
    - tool_outputs_dir: 超长 content 的落盘根目录（实际写到 `<dir>/<trace_id>/`）。

    凭证缺失时两个工具都返回 `not_configured`，不做任何降级。
    """
    outputs_dir = Path(tool_outputs_dir).expanduser()

    base_url = (_as_str(getattr(settings, "search_api_base_url", "")) or _DEFAULT_BASE_URL).strip()
    base_url = base_url.rstrip("/") or _DEFAULT_BASE_URL
    api_key = (getattr(settings, "deepseek_search_api_key", "") or "").strip()
    timeout_s = float(getattr(settings, "web_search_timeout_s", 30.0) or 30.0)
    # 契约 §7.7：签名默认值（count=5 / need_content=False）就是契约本身；
    # config.py 里不再保留同义字段，避免出现两套默认值。要改默认值须走契约变更流程，
    # 把签名改成 None 哨兵并同步文档。

    max_lines = _as_int(getattr(settings, "tool_output_max_lines", 2000)) or 2000
    max_bytes = _as_int(getattr(settings, "tool_output_max_bytes", 64 * 1024)) or 64 * 1024
    head_lines = _as_int(getattr(settings, "tool_output_head_lines", 30)) or 30
    tail_lines = _as_int(getattr(settings, "tool_output_tail_lines", 60)) or 60

    def _not_configured() -> dict[str, Any]:
        return tool_error(
            "not_configured",
            "未配置 DEEPSEEK_SEARCH_API_KEY，WebSearch/WebFetch 不可用",
            hint=_HINT["not_configured"],
            retryable=False,
            attempts=0,
        )

    def _stub_context() -> tuple[str, str]:
        return current_trace_id.get() or "notrace", current_tool_call_id.get() or ulid()

    # ------------------------------------------------------------ WebSearch
    @tool
    def WebSearch(query: str, count: int = 5, need_content: bool = False) -> dict[str, Any]:
        """联网搜索（无状态：每次实时请求 /v1/tools/search，不缓存）。

        入参：
        - query: 搜索词，1–128 字符；支持 site: / -site: / -词 / "短语" / lang: / after: / before:；
          不做二次改写、不加隐式引号
        - count: 返回条数 1–50（默认 5，由签名固定）；
          越界按边界截断，并在返回的 count_note 里注明
        - need_content: 是否要正文（默认 false，由签名固定）。
          false 时单条 content 为 ""，但 summary 仍有值（实测 1988–2870 字符，波动，可当摘要）；
          true 时单条 content 可达 1.5 万字符，多条会顶爆上下文

        选路（务必遵守，否则会重复付费）：
        - 已经知道要查什么 → 直接用 `need_content=true` 一次拿多条正文，
          **不要** search 完再对每条结果调 WebFetch（两者正文高度重叠，同一批内容付两次费）；
        - 只有已知具体 URL、或只缺该页完整正文时才用 WebFetch。

        返回：
        {"ok": true, "query", "count", "need_content", "results": [
          {"title","url","snippet","summary","content","site_name","site_icon",
           "authority","rerank_score","date"}]}
        content 超过 2000 行 / 64 KB 时落盘为 tool_outputs/<trace_id>/<tool_call_id>-<n>.md，
        content 字段替换为与 Shell/Read 相同的 stub 文本（附 artifact 绝对路径）。
        失败返回 {"ok": false, "error_class", "message", "hint", "retryable", "attempts"}：
        error_class ∈ not_configured / invalid_args / auth_error / insufficient_balance /
        forbidden / not_found / timeout / rate_limited / upstream_error / network_error /
        http_error；其中 401 auth_error 与 402 insufficient_balance 永不重试。
        """
        invalid = _validate_query(query)
        if invalid is not None:
            return invalid

        try:
            effective_count = int(count)
        except (TypeError, ValueError):
            return _local_failure("count 必须是整数")
        count_note = ""
        if effective_count < _MIN_COUNT:
            count_note = f"count={effective_count} 低于下限，已截断为 {_MIN_COUNT}"
            effective_count = _MIN_COUNT
        elif effective_count > _MAX_COUNT:
            count_note = f"count={effective_count} 超过上限，已截断为 {_MAX_COUNT}"
            effective_count = _MAX_COUNT

        if not api_key:
            return _not_configured()

        want_content = bool(need_content)
        payload = {"query": query, "count": effective_count, "need_content": want_content}
        response = _call_json(
            f"{base_url}{_SEARCH_PATH}",
            payload,
            api_key=api_key,
            timeout_s=timeout_s,
        )
        if not response.get("ok"):
            return response

        raw_results = response["data"].get("results")
        if not isinstance(raw_results, list):
            return _shape_failure("返回体缺少 results 数组", status=200)

        trace_id, call_id = _stub_context()
        results: list[dict[str, Any]] = []
        for index, item in enumerate(raw_results, start=1):
            normalized = _normalize_result(item)
            content, artifact = _maybe_stub(
                normalized["content"],
                header=f"[web] WebSearch 正文过长已落盘：{normalized['url']}",
                index=index,
                outputs_dir=outputs_dir,
                trace_id=trace_id,
                call_id=call_id,
                max_lines=max_lines,
                max_bytes=max_bytes,
                head_lines=head_lines,
                tail_lines=tail_lines,
            )
            normalized["content"] = content
            if artifact is not None:
                normalized["artifact"] = artifact
            results.append(normalized)

        out: dict[str, Any] = {
            "ok": True,
            "query": query,
            "count": effective_count,
            "need_content": want_content,
            "results": results,
        }
        if count_note:
            out["count_note"] = count_note
        return out

    # ------------------------------------------------------------ WebFetch
    @tool
    def WebFetch(url: str) -> dict[str, Any]:
        """抓取单个网页并转成 Markdown（无状态：每次实时请求 /v1/tools/fetch，不缓存）。

        入参：
        - url: 必须带 host 的 http/https，≤8192 字符；含账号密码的 URL 直接拒绝（不发请求）

        选路：本工具只用于「已有具体 URL」或「只缺该页完整正文」。
        若还在「要查什么」的阶段，用 WebSearch(need_content=true) 一次拿多条正文，
        **不要** search 完再逐条 WebFetch——返回正文高度重叠，会重复付费。

        返回：
        {"ok": true, "url", "title", "content", "truncated"}
        content 为 Markdown；超过 2000 行 / 64 KB 时落盘为
        tool_outputs/<trace_id>/<tool_call_id>-<n>.md，content 替换为与 Shell/Read
        相同的 stub 文本（truncated=true，并附 artifact 绝对路径）。
        失败返回 {"ok": false, "error_class", "message", "hint", "retryable", "attempts"}：
        404 not_found 表示抓不到正文（需登录/反爬），可改用 WebSearch 的 summary；
        401 auth_error 与 402 insufficient_balance 永不重试。
        """
        invalid = _validate_url(url)
        if invalid is not None:
            return invalid
        if not api_key:
            return _not_configured()

        response = _call_json(
            f"{base_url}{_FETCH_PATH}",
            {"url": url},
            api_key=api_key,
            timeout_s=timeout_s,
        )
        if not response.get("ok"):
            return response

        data = response["data"]
        title = _as_str(data.get("title"))
        content = _as_str(data.get("content"))

        trace_id, call_id = _stub_context()
        content, artifact = _maybe_stub(
            content,
            header=f"[web] WebFetch 正文过长已落盘：{url}",
            index=1,
            outputs_dir=outputs_dir,
            trace_id=trace_id,
            call_id=call_id,
            max_lines=max_lines,
            max_bytes=max_bytes,
            head_lines=head_lines,
            tail_lines=tail_lines,
        )
        out: dict[str, Any] = {
            "ok": True,
            "url": url,
            "title": title,
            "content": content,
            "truncated": artifact is not None,
        }
        if artifact is not None:
            out["artifact"] = artifact
        return out

    return [WebSearch, WebFetch]
