"""M-WEB 验收：正常搜索、need_content 两态、fetch、错误码映射、not_configured、
超长落盘、凭证隔离，以及可选的真实调用（需要 DEEPSEEK_SEARCH_API_KEY）。

用法：
    python scripts/check_web.py              # 有 key 时附带真实调用
    python scripts/check_web.py --offline    # 只用桩，不访问网络
    # worktree 里没有 .env 时，用 ASSISTANT_ENV_FILE 指向主仓库的 .env：
    #   $env:ASSISTANT_ENV_FILE = "C:\\...\\personal-assistant\\.env"
    #   python scripts/check_web.py

退出码 0 才算交付。真实调用只打印脱敏摘要（前 5 位 + 后 4 位）。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import Settings, settings as env_settings  # noqa: E402
from agents.context import current_tool_call_id, current_trace_id  # noqa: E402
from tools import shell as shell_tools  # noqa: E402
from tools import web  # noqa: E402


class FakeHttp:
    """替换 `web._post_json`：按队列返回预设响应，并记录每次请求。"""

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, payload: dict[str, Any], *, api_key: str, timeout_s: float):
        self.calls.append(
            {"url": url, "payload": dict(payload), "api_key": api_key, "timeout_s": timeout_s}
        )
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        response = self.responses[index]
        if callable(response):
            response = response(url, payload)
        if isinstance(response, web._HttpResult):
            return response
        status, body = response
        if not isinstance(body, str):
            body = json.dumps(body, ensure_ascii=False)
        return web._HttpResult(status, body)


def make_settings(**overrides: Any) -> Settings:
    """构造测试用 Settings；显式 kwargs 优先于 .env。"""
    base: dict[str, Any] = {
        "deepseek_search_api_key": "sk-search-test",
        "search_api_base_url": "https://api.deepseek.com",
        "llm_api_key": "sk-llm-must-not-be-used",
        "llm_base_url": "https://api.deepseek.com",
        "web_search_timeout_s": 5.0,
    }
    base.update(overrides)
    return Settings(**base)


def build(settings: Settings, outputs: Path):
    search, fetch = web.build_web_tools(settings=settings, tool_outputs_dir=outputs)
    return search, fetch


def search_body(contents: list[str] | None = None) -> dict[str, Any]:
    contents = contents or [""]
    results = []
    for i, content in enumerate(contents, start=1):
        results.append(
            {
                "title": f"标题{i}",
                "url": f"https://example.com/{i}",
                "snippet": f"片段{i}",
                "summary": f"摘要{i}·" + "说明" * 60,
                "content": content,
                "site_name": "example",
                "site_icon": "https://example.com/icon.png",
                "authority": i,
                "rerank_score": 0.9 - i / 100,
                "date": "2025-10-08T16:00:00+00:00",
            }
        )
    return {"results": results}


def main() -> int:
    offline = "--offline" in sys.argv
    failures: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail and not cond else ""))
        if not cond:
            failures.append(name)

    tmp = Path(tempfile.mkdtemp(prefix="asst_web_check_"))
    outputs = tmp / "tool_outputs"
    original_post = web._post_json
    original_sleep = web._RETRY_SLEEP_S
    web._RETRY_SLEEP_S = 0.0
    current_trace_id.set("trace-web")
    current_tool_call_id.set("call-001")

    try:
        # ---------------------------------------------------------------- not_configured
        search, fetch = build(make_settings(deepseek_search_api_key=""), outputs)
        r = search.invoke({"query": "deepseek"})
        check(
            "not_configured.search",
            r.get("ok") is False
            and r.get("error_class") == "not_configured"
            and r.get("attempts") == 0
            and r.get("retryable") is False,
            str(r),
        )
        check("not_configured.no_llm_fallback", "LLM_API_KEY" in str(r.get("hint", "")), str(r.get("hint")))
        r = fetch.invoke({"url": "https://example.com"})
        check(
            "not_configured.fetch",
            r.get("error_class") == "not_configured" and r.get("attempts") == 0,
            str(r),
        )

        # ---------------------------------------------------------------- 凭证隔离
        source = Path(web.__file__).read_text(encoding="utf-8")
        check("isolation.no_llm_field", "llm_api_key" not in source and "llm_base_url" not in source)
        check("isolation.only_search_key", "deepseek_search_api_key" in source)
        check("isolation.no_env_read", "getenv" not in source and "environ" not in source)

        fake = FakeHttp([(200, search_body())])
        web._post_json = fake  # type: ignore[assignment]
        search, fetch = build(make_settings(), outputs)
        r = search.invoke({"query": "凭证隔离"})
        check("isolation.uses_search_key", r.get("ok") is True, str(r))
        check(
            "isolation.bearer",
            bool(fake.calls)
            and fake.calls[0]["api_key"] == "sk-search-test"
            and fake.calls[0]["api_key"] != "sk-llm-must-not-be-used",
            str(fake.calls[:1]),
        )
        check(
            "isolation.endpoint",
            bool(fake.calls)
            and fake.calls[0]["url"] == "https://api.deepseek.com/v1/tools/search",
            str(fake.calls[:1]),
        )
        check("stub.reuse", web.stub_text is shell_tools.stub_text and web.needs_artifact is shell_tools.needs_artifact)

        # ---------------------------------------------------------------- 正常搜索（need_content=false）
        r = search.invoke({"query": "DeepSeek API 文档"})
        check("search.ok", r.get("ok") is True, str(r))
        check(
            "search.request",
            fake.calls[-1]["payload"] == {"query": "DeepSeek API 文档", "count": 5, "need_content": False},
            str(fake.calls[-1]["payload"]),
        )
        check(
            "search.meta",
            r.get("query") == "DeepSeek API 文档" and r.get("count") == 5 and r.get("need_content") is False,
            str({k: r.get(k) for k in ("query", "count", "need_content")}),
        )
        results = r.get("results") or []
        check("search.results_count", len(results) == 1, str(len(results)))
        expected_keys = {
            "title", "url", "snippet", "summary", "content", "site_name", "site_icon",
            "authority", "rerank_score", "date",
        }
        check("search.result_fields", expected_keys <= set(results[0]), str(sorted(results[0])))
        check(
            "search.no_content_but_summary",
            results[0]["content"] == "" and len(results[0]["summary"]) > 100,
            f"content={len(results[0]['content'])} summary={len(results[0]['summary'])}",
        )
        check("search.no_count_note", "count_note" not in r)

        # ---------------------------------------------------------------- need_content=true
        fake_true = FakeHttp([(200, search_body(["# 正文\n\nMarkdown 内容 " * 50]))])
        web._post_json = fake_true  # type: ignore[assignment]
        search, _ = build(make_settings(), outputs)
        r = search.invoke({"query": "正文测试", "need_content": True})
        check("search.need_content.request", fake_true.calls[0]["payload"]["need_content"] is True)
        check("search.need_content.content", r.get("ok") and len(r["results"][0]["content"]) > 500, str(r)[:200])
        check("search.need_content.meta", r.get("need_content") is True)

        # ---------------------------------------------------------------- count 越界截断
        fake = FakeHttp([(200, search_body())])
        web._post_json = fake  # type: ignore[assignment]
        search, _ = build(make_settings(), outputs)
        r = search.invoke({"query": "count 下限", "count": 0})
        check(
            "count.clamp_low",
            r.get("count") == 1 and "下限" in str(r.get("count_note", "")) and fake.calls[0]["payload"]["count"] == 1,
            str({k: r.get(k) for k in ("count", "count_note")}),
        )
        r = search.invoke({"query": "count 上限", "count": 999})
        check(
            "count.clamp_high",
            r.get("count") == 50 and "上限" in str(r.get("count_note", "")) and fake.calls[1]["payload"]["count"] == 50,
            str({k: r.get(k) for k in ("count", "count_note")}),
        )

        # ---------------------------------------------------------------- WebFetch 成功
        fake = FakeHttp([(200, {"title": "示例页面", "content": "# 标题\n\n正文段落"})])
        web._post_json = fake  # type: ignore[assignment]
        _, fetch = build(make_settings(), outputs)
        r = fetch.invoke({"url": "https://example.com/page"})
        check("fetch.ok", r.get("ok") is True, str(r))
        check(
            "fetch.request",
            fake.calls[0]["url"] == "https://api.deepseek.com/v1/tools/fetch"
            and fake.calls[0]["payload"] == {"url": "https://example.com/page"},
            str(fake.calls[:1]),
        )
        check(
            "fetch.body",
            r.get("title") == "示例页面" and "正文段落" in r.get("content", "") and r.get("truncated") is False,
            str({k: r.get(k) for k in ("title", "truncated")}),
        )

        # ---------------------------------------------------------------- 错误码映射
        # (status, error_class, 可重试, 期望调用次数)
        error_cases = [
            (400, "invalid_args", False, 1),
            (401, "auth_error", False, 1),
            (402, "insufficient_balance", False, 1),
            (403, "forbidden", False, 1),
            (404, "not_found", False, 1),
            (408, "timeout", True, 2),
            (413, "invalid_args", False, 1),
            (429, "rate_limited", True, 2),
            (500, "upstream_error", True, 2),
            (503, "upstream_error", True, 2),
            (418, "http_error", False, 1),
        ]
        for status, expected_class, retryable, expected_calls in error_cases:
            fake = FakeHttp([(status, {"error": {"message": f"boom-{status}"}})])
            web._post_json = fake  # type: ignore[assignment]
            search, _ = build(make_settings(), outputs)
            r = search.invoke({"query": "错误码"})
            check(
                f"error.{status}.class",
                r.get("error_class") == expected_class,
                f"{r.get('error_class')} != {expected_class}",
            )
            check(f"error.{status}.retryable", r.get("retryable") is retryable, str(r.get("retryable")))
            check(
                f"error.{status}.attempts",
                len(fake.calls) == expected_calls and r.get("attempts") == expected_calls,
                f"calls={len(fake.calls)} attempts={r.get('attempts')}",
            )
            check(f"error.{status}.server_text", f"boom-{status}" in str(r.get("message", "")), str(r.get("message")))
            if status in (401, 402):
                check(f"error.{status}.no_retry", len(fake.calls) == 1, str(len(fake.calls)))
            if status in (408, 429, 500, 503):
                check(f"error.{status}.status_code", r.get("status_code") == status, str(r.get("status_code")))

        # 5xx 重试后成功
        fake = FakeHttp(
            [
                (503, {"error": "temporary"}),
                (200, search_body(["恢复后的正文"])),
            ]
        )
        web._post_json = fake  # type: ignore[assignment]
        search, _ = build(make_settings(), outputs)
        r = search.invoke({"query": "重试后成功", "need_content": True})
        check(
            "retry.recover",
            r.get("ok") is True and len(fake.calls) == 2 and r["results"][0]["content"] == "恢复后的正文",
            str(r)[:200],
        )

        # 2xx 但返回体结构不对（不可重试）
        fake = FakeHttp([(200, "not-json")])
        web._post_json = fake  # type: ignore[assignment]
        search, _ = build(make_settings(), outputs)
        r = search.invoke({"query": "坏 JSON"})
        check(
            "shape.invalid_json",
            r.get("error_class") == "upstream_error" and r.get("retryable") is False and len(fake.calls) == 1,
            str(r),
        )
        fake = FakeHttp([(200, {"nope": True})])
        web._post_json = fake  # type: ignore[assignment]
        search, _ = build(make_settings(), outputs)
        r = search.invoke({"query": "缺 results"})
        check(
            "shape.missing_results",
            r.get("error_class") == "upstream_error" and r.get("retryable") is False and len(fake.calls) == 1,
            str(r),
        )

        # 传输层：超时 / 网络异常（各重试 1 次）
        fake = FakeHttp([web._HttpResult(None, kind="timeout", detail="请求超时: read timeout")])
        web._post_json = fake  # type: ignore[assignment]
        search, _ = build(make_settings(), outputs)
        r = search.invoke({"query": "超时"})
        check(
            "transport.timeout",
            r.get("error_class") == "timeout" and r.get("retryable") is True and len(fake.calls) == 2,
            str(r.get("error_class")) + f" calls={len(fake.calls)}",
        )
        fake = FakeHttp([web._HttpResult(None, kind="network", detail="网络异常: connect refused")])
        web._post_json = fake  # type: ignore[assignment]
        search, _ = build(make_settings(), outputs)
        r = search.invoke({"query": "断网"})
        check(
            "transport.network",
            r.get("error_class") == "network_error" and len(fake.calls) == 2,
            str(r.get("error_class")) + f" calls={len(fake.calls)}",
        )

        # ---------------------------------------------------------------- 本地参数校验（不发请求）
        fake = FakeHttp([(200, search_body())])
        web._post_json = fake  # type: ignore[assignment]
        search, fetch = build(make_settings(), outputs)
        local_cases = [
            ("local.query_empty", lambda: search.invoke({"query": ""})),
            ("local.query_blank", lambda: search.invoke({"query": "   "})),
            ("local.query_too_long", lambda: search.invoke({"query": "x" * 129})),
            ("local.url_empty", lambda: fetch.invoke({"url": ""})),
            ("local.url_scheme", lambda: fetch.invoke({"url": "ftp://example.com/a"})),
            ("local.url_no_host", lambda: fetch.invoke({"url": "https://"})),
            ("local.url_credentials", lambda: fetch.invoke({"url": "https://user:pass@example.com/a"})),
            ("local.url_too_long", lambda: fetch.invoke({"url": "https://example.com/" + "a" * 8200})),
            ("local.url_space", lambda: fetch.invoke({"url": "https://example.com/a b"})),
        ]
        for name, invoke in local_cases:
            r = invoke()
            check(
                name,
                r.get("error_class") == "invalid_args" and r.get("attempts") == 0,
                str(r),
            )
        check("local.no_requests", fake.calls == [], str(fake.calls[:1]))

        # ---------------------------------------------------------------- 超长落盘（复用 stub）
        long_content = "\n".join(f"L{i:04d}" for i in range(1, 2501))
        fake = FakeHttp([(200, search_body([long_content]))])
        web._post_json = fake  # type: ignore[assignment]
        search, _ = build(make_settings(), outputs)
        current_trace_id.set("trace-web")
        current_tool_call_id.set("call-long-search")
        r = search.invoke({"query": "超长正文", "need_content": True})
        item = (r.get("results") or [{}])[0]
        artifact = Path(item.get("artifact", ""))
        check("stub.search.truncated", r.get("ok") is True and "[artifact]" in item.get("content", ""), str(r.get("error_class")))
        check(
            "stub.search.path",
            artifact.is_absolute()
            and artifact == (outputs / "trace-web" / "call-long-search-1.md").resolve(),
            str(artifact),
        )
        check("stub.search.file", artifact.is_file(), str(artifact))
        if artifact.is_file():
            full = artifact.read_text(encoding="utf-8")
            check(
                "stub.search.complete",
                len(full.splitlines()) == 2500 and "L0500" in full and "L2500" in full,
            )
        body = item.get("content", "")
        check(
            "stub.search.head_tail",
            "L0001" in body and "L0030" in body and "L2500" in body and "L0500" not in body,
            body[:200],
        )
        check("stub.search.hint", "Grep" in body and "Read" in body)
        check(
            "stub.search.keeps_locator",
            item.get("url") == "https://example.com/1" and len(item.get("summary", "")) > 0
            and not (outputs / "trace-web" / "call-long-search-2.md").exists(),
        )

        # WebFetch 超长（字节阈值）
        big_markdown = "段落" * 20000  # 约 120 KB
        fake = FakeHttp([(200, {"title": "大页面", "content": "\n".join([big_markdown] * 2)})])
        web._post_json = fake  # type: ignore[assignment]
        _, fetch = build(make_settings(), outputs)
        current_tool_call_id.set("call-long-fetch")
        r = fetch.invoke({"url": "https://example.com/big"})
        artifact = Path(r.get("artifact", ""))
        check(
            "stub.fetch.truncated",
            r.get("ok") is True and r.get("truncated") is True and "[artifact]" in r.get("content", ""),
            str(r.get("error_class")),
        )
        check(
            "stub.fetch.path",
            artifact == (outputs / "trace-web" / "call-long-fetch-1.md").resolve() and artifact.is_file(),
            str(artifact),
        )

        # ---------------------------------------------------------------- 真实调用
        web._post_json = original_post  # type: ignore[assignment]  # 恢复真实 HTTP
        if offline:
            print("SKIP(真实调用：--offline)")
        elif not env_settings.deepseek_search_api_key:
            print(
                "SKIP(真实调用：未配置 DEEPSEEK_SEARCH_API_KEY；"
                "worktree 下请设 ASSISTANT_ENV_FILE 指向主仓库 .env)"
            )
        else:
            live_outputs = tmp / "live_outputs"
            search, fetch = build(env_settings, live_outputs)
            current_trace_id.set("trace-live")
            current_tool_call_id.set("live-search")
            print(f"[LIVE] key={web.mask_api_key(env_settings.deepseek_search_api_key)}")

            t0 = time.perf_counter()
            r = search.invoke({"query": "DeepSeek API 文档", "count": 3})
            elapsed = time.perf_counter() - t0
            check("live.search.ok", r.get("ok") is True, str(r)[:300])
            if r.get("ok"):
                titles = [item["title"][:20] for item in r["results"]]
                print(
                    f"[LIVE] search(need_content=false) {elapsed:.2f}s "
                    f"results={len(r['results'])} summaries={[len(i['summary']) for i in r['results']]} "
                    f"contents={[len(i['content']) for i in r['results']]} titles={titles}"
                )

            current_tool_call_id.set("live-search-content")
            t0 = time.perf_counter()
            r2 = search.invoke({"query": "DeepSeek API 文档", "count": 3, "need_content": True})
            elapsed = time.perf_counter() - t0
            check("live.search_content.ok", r2.get("ok") is True, str(r2)[:300])
            if r2.get("ok"):
                print(
                    f"[LIVE] search(need_content=true) {elapsed:.2f}s "
                    f"contents={[len(i['content']) for i in r2['results']]} "
                    f"artifacts={[bool(i.get('artifact')) for i in r2['results']]}"
                )

            target = next(
                (item["url"] for item in (r.get("results") or []) if item.get("url")),
                "https://api-docs.deepseek.com/",
            )
            current_tool_call_id.set("live-fetch")
            t0 = time.perf_counter()
            r3 = fetch.invoke({"url": target})
            elapsed = time.perf_counter() - t0
            check("live.fetch.ok", r3.get("ok") is True, str(r3)[:300])
            if r3.get("ok"):
                print(
                    f"[LIVE] fetch {elapsed:.2f}s url={target} title_len={len(r3['title'])} "
                    f"content_len={len(r3['content'])} truncated={r3['truncated']}"
                )
    finally:
        web._post_json = original_post  # type: ignore[assignment]
        web._RETRY_SLEEP_S = original_sleep
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
