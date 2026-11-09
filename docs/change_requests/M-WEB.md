# M-WEB 契约变更请求

分支：`feat/web` @ `personal-assistant-dev-web`　　模块：M-WEB

以下各项与 `docs/ENGINEERING_CONTRACT.md` 的**文字**不一致（冻结文档本身未改，请主线裁定后统一更新）。
本次实现以 §7.7 为准，其余为文字层面的冲突/缺口。

## 1. 工厂签名两处并存（§7.5 vs §7.7）

- **§7.5 原文**：`build_web_tools(*, api_key, base_url="https://api.deepseek.com", tool_outputs_dir, timeout_s=30)`。
- **§7.7 原文**：`build_web_tools(*, settings: Settings, tool_outputs_dir: Path)`。
- **实际实现**：按 §7.7（`settings` 版本）。理由：凭证隔离要求只读 `settings.deepseek_search_api_key`，
  把 `api_key` 作为入参会让装配层有机会误传 LLM key。
- **建议**：删除 §7.5 的旧签名，或注明"以 §7.7 为准"。

## 2. 默认值：签名写死 vs 读 settings（§7.7 内部冲突）

- **签名**：`WebSearch(query, count: int = 5, need_content: bool = False)`。
- **条目说明**："`count` 默认取 `settings.web_search_default_count`；`need_content` 默认取
  `settings.web_search_need_content`"。
- **问题**：`@tool` 的 schema 默认值在装饰时固定，函数内无法区分"未传"与"显式传 5/False"；
  若两者都读 settings，只能把签名改成 `count=None` / `need_content=None`，与冻结签名不符。
- **实际实现**：保留冻结签名（5 / False）。当前 `config.py` 的
  `web_search_default_count=5`、`web_search_need_content=False` 与之完全一致，行为无差异。
- **建议**：二选一——(a) 明确"签名默认值即契约，config 两项仅作文档/装配提示"；
  (b) 若以后要调大默认条数，走变更流程把签名改成 `None` 哨兵并同步文档。

## 3. stub 复用位置写错（§7.7 落盘 stub 规则）

- **§7.7 原文**："复用 `tools/paths.py` 的格式化函数"。
- **实际**：`tools/paths.py` 只有路径守卫，没有任何格式化函数；`stub_text()` / `needs_artifact()`
  在 `tools/shell.py`（§7.5 的表述是正确的）。
- **实际实现**：`tools/web.py` 直接 `from tools.shell import needs_artifact, stub_text`，
  输出格式与 Shell/Read 完全一致（头 30 + 尾 60 + `[artifact]` 绝对路径 + Read/Grep 提示），
  未新写 stub。验收脚本断言 `web.stub_text is shell_tools.stub_text`。
- **建议**：把 §7.7 的 "`tools/paths.py`" 改为 "`tools/shell.py`"。

## 4. 错误类清单两套（§7.5 vs §7.7）

- **§7.5 原文**：失败返回 `error_class ∈ {http_error, network, quota}`。
- **§7.7 表格**：`not_configured / invalid_args / auth_error / insufficient_balance / forbidden /
  not_found / timeout / rate_limited / upstream_error / network_error / http_error`，
  并给出各自可重试性。
- **实际实现**：按 §7.7 表格；表格未列出的 4xx 归 `http_error`（不可重试）；
  2xx 但返回体不是合法 JSON/缺 `results` 归 `upstream_error`（**不可重试**，避免格式问题反复刷额度）。
- **建议**：删除 §7.5 的三类旧表述，只保留 §7.7 表格。

## 5. 落盘路径依赖未装配的上下文（备案，不一定改契约）

§7.7 要求落盘到 `tool_outputs/<trace_id>/<tool_call_id>-<n>.md`。`agents/context.py` 已定义
`current_tool_call_id`，但当前仓库**没有任何代码给它赋值**（`current_trace_id` 由 router/worker 设置）。
本实现读这两个 ContextVar；`current_tool_call_id` 为空时退回 `ulid()`，trace 为空时目录用 `notrace/`。
**建议**：装配层在每次工具调用前 `current_tool_call_id.set(tool_call_id)`，否则文件名退化为随机 ULID。

## 6. 返回字段补充（备案）

§7.7 的返回块未写 `truncated` / `artifact`，而 §7.5 写了 `truncated`。本实现两者都返回
（`truncated` 表示 content 已落盘替换为 stub），属向后兼容的增字段，无需改契约。
