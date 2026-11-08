# M-FS 契约变更请求

分支：`feat/fs` @ `personal-assistant-dev-fs`　　模块：M-FS

以下两项与 `docs/ENGINEERING_CONTRACT.md` 的**文字**不一致（契约文件本身未改，请主线裁定后更新冻结文档）。

## 1. `Read` 的图片返回（§7.3 表格）

- **契约原文**：图片返回内容块，但"v1 先返回 `{code:"image_not_supported"}`，真实图片通道留到实验后"。
- **本次任务要求**：`Read` 支持图片并返回可被多模态模型消费的形式。
- **实际实现**：png/jpg/jpeg/gif/webp/bmp 返回工具消息内容块列表
  `[{"type":"text","text":...}, {"type":"image_url","image_url":{"url":"data:<mime>;base64,..."}}]`；
  原图 > 4 MB 返回 `error_class="image_too_large"`（不做自动缩放，避免引入 Pillow 依赖）；
  `as_="text"` 可强制按文本读（此时二进制探测会返回 `binary_file`）。
- **影响面**：`tools.fs` 的调用方（router/worker 装配）无需改签名；但**文本模型**收到 image 块可能 400，
  需要装配层按模型能力决定是否传 `as_="text"`。
- **建议**：把 §7.3 的图片说明改为上述行为，并补一句"文本模型请传 `as_='text'`"。

## 2. `Grep` 的行号开关（§7.3 表格）

- **契约原文**：入参写作 `-n=True`。
- **问题**：`-n` 不是合法的 Python/JSON 参数名，`@tool` 无法生成该字段。
- **实际实现**：参数名 `n: bool = True`（语义等价 `grep -n`），docstring 已注明。
- **建议**：契约表格改为 `n=True（等价 grep -n）`。

## 3. `Glob` 的截断/空结果表达（无需改契约，仅备案）

契约要求返回 `list[str]`，但大仓库下需要截断提示。实现为：
正常匹配返回绝对路径列表（mtime 新→旧）；空结果返回单元素 `["[no matches] ..."]`；
超限时在列表末尾追加一条 `"[已截断] 共匹配 N 个..."`。若主线希望改为
`{"matches": [...], "truncated": bool}`，请下发新契约，本模块再改。
