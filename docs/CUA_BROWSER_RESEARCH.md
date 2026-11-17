# 浏览器方向 CUA（Computer Use Agent）技术调研

> 调研对象：给 `create_session` 增加 `task_kind="cua"`（浏览器操作）这一支的可行性。
> 调研日期：2026-11-09。本文只做调研与设计建议，**不改任何代码**。
>
> 证据标记：
> - **【代码】** 直接来自当前仓库实现，附文件位置；
> - **【外部】** 公开文档 / 厂商博客 / 第三方测量；
> - **【实测】** 2026-11-13 在本机、当前模型上跑出来的数据，附脚本与产物路径；
> - **【估算】** 基于前两类证据的推算，给出算式；
> - **【未验证】** 需要在真机或真模型上做实验才能确认，本文不把它当结论。

## 修订记录（2026-11-13）

原文写于 2026-11-09，其中三处事实与一处核心论证已经过时。本次**先改事实，再改结论**，每处改动都在原位置标注 `【订正 2026-11-13】`，不静默重写。

| # | 原文断言 | 复核结果 | 处置 |
|---|---|---|---|
| 1 | 当前 `llm_supports_vision=False`，纯视觉"根本跑不起来" | 开关默认 `True`【代码 `config.py:61`】，`.env` 也是 `LLM_SUPPORTS_VISION=true`；实跑让模型读截图标题，正确返回 `Google Chrome` | 删掉该论据；视觉能力不再是路线否决项 |
| 2 | "`ToolMessage` 里放 `image_url` 是否合法"未核实 | 已按端点限制改造：工具只回文本引用，请求前由 `inject_image_blocks` 追加为**独立 user 消息**（tool 角色带图会 400）；`check_fs.py` 69/69 通过 | 从"未验证"改为"已验证"，见 §1.2 |
| 3 | worker recursion limit 40，20–30 步"余量不大" | 现为 300【代码 `config.py:85-86`】 | 不再作为选型理由；步数预算改为成本/延迟考虑 |
| 4 | "ref 式比纯截图省 token" | **被实测推翻为"看页面"**：1280×800 截图固定 **643 token**；a11y 快照从 **58** 到 **131,828** token 不等（§11） | 结论重写为"分页面类型"，并保留定位精度等并列维度 |

---


## 0. 结论先行

### 0.1 推荐路线

**推荐第 ③ 条：混合路线，DOM / accessibility tree 为主，视觉按需兜底。**

具体是：

1. 用 Playwright 在 worker 进程内启动一个**独立、持久化 profile 的 Chromium**；
2. 每步默认返回**裁剪过的 accessibility snapshot（带 `ref` 编号）**，模型点 `ref`，不点坐标；
3. 截图是**独立工具、默认关闭**，只在 canvas / 图表 / 布局歧义 / 点击失败时调用；
4. 截图**默认只落盘返回路径**，不把 base64 塞进 messages；
5. 复用现有 `SendMessage` 做进度与求助，复用 `StopCommand` 三级终止，复用 `ContextBudgetMiddleware` 做折叠，但需要一套 **CUA 专用预算策略**（见 §5.3）。

不建议 v1 做纯视觉（截图 + 坐标点击）作为主路径。**【订正 2026-11-13】原文的三条理由已重写**——"视觉不可用"和"图片一定更贵"都不成立：

- ~~当前仓库配置 `llm_supports_vision=False`，纯视觉路线在现配置下根本跑不起来~~：现为 `True`【代码 `config.py:61`】，`.env` 是 `LLM_SUPPORTS_VISION=true`，且实测模型能读出截图内容（§11，返回 `Google Chrome`，`usage.prompt_tokens=696`）。
- ~~纯视觉每步多 1–2k 图片 token，成本会放大~~：实测当前模型 1280×800 截图固定 **643 token**（同尺寸 JPEG q80 也是 643），比原文假设低；在信息密集的长文页上，整页 a11y 快照反而是 **131,828 token**，截图便宜两个数量级（§11）。
- **仍然成立的理由**：截图只覆盖当前视口（长页面需要多屏拼接才能看全）；坐标点击对滚动、弹窗、懒加载、分辨率变化很脆，稳定性低于 ref 点击【外部 Playwright MCP 文档的对比表】；且失败时无法解释"为什么点这里"。

**推论变化**：推荐 ③ 混合路线的首要理由，从"ref 更省 token"改为"**ref 定位确定、可回放**"；同时"每步发整页快照"同样不可取，必须先裁剪或用 `find` 缩小范围（§11 的 Wikipedia 数据）。

### 0.2 值不值得做

**分场景，结论不同：**

| 场景 | 结论 |
|---|---|
| 用户每周有 ≥3 次"必须在某个没有 API、需要登录/点击/填表的网站上操作"的重复任务 | **值得做 v1**，范围收窄到 8–9 个浏览器工具 + 1 个 worker 类型，投入可控 |
| 只是偶尔查资料、读公开页面 | **不值得**。现有 `WebFetch` / `WebSearch` 更快、更便宜、更稳 |
| 想做"通用 CUA / 任意网站 / 代替人操作电脑" | **现阶段不值得**。稳定性、延迟/成本（失败重跑）仍不支持；**视觉能力本身不是阻塞项**（已实测可用），见 §11 |

一句话：**"浏览器当工具"值得做，"通用视觉 CUA"现在不值得做。** 本文的 v1 建议是前者。

### 0.3 三个前置条件的当前状态（2026-11-13 复核）

~~原文要求：三个必须先验证的前置条件（P0，未验证）。~~ **【订正 2026-11-13】前两项已完成，第三项已有实测数据：**

1. **模型能不能收图** —— **已验证**。`llm_supports_vision` 默认 `True`【代码 `config.py:61`】，`.env` 为 `true`；把真实截图发给当时使用的 `GLM-5.3-Flash`（经本地网关，该网关已于 2026-11-17 移除），不报 400，`usage.prompt_tokens=696`，模型正确回答截图中标题为 `Google Chrome`（§11）。
2. **工具返回带图片是否合法** —— **已解决，且实现方式改了**。当前 `Read` 只返回文本引用、不内联 base64【代码 `tools/fs.py:256-282`】；请求前由 `VisionAdapterMiddleware` 还原成**独立 user 消息**里的 `image_url`【代码 `agents/vision.py:134-191`、`agents/middleware.py:79-87`】。原因写在 `agents/vision.py:7-12`：端点只允许图片出现在 user 消息，tool 角色带图会 400。`scripts/check_fs.py` 实跑 **69 通过 / 0 失败**，其中 7 条覆盖图片通路。
3. **典型站点的 snapshot 体积** —— **已测 4 类页面**（长文文档、表单、列表应用、画布应用）。结论与原文假设差距很大：整页 a11y 快照 58–131,828 token，不是稳定的 2k–10k（§11）。裁剪策略仍然必须做，但预算要按页面类型重算。

---

## 1. 现有架构的约束（代码证据）

这一节只列与 CUA 直接相关的事实，避免后面重复。

### 1.1 worker 的装配方式

- worker 由 `create_session` 拉起独立进程，读 `workers/<task_id>/task.json`，跑 LangGraph，结果通过 `worker_result` 回 Redis【代码 `worker_runner.py`、`tools/sessions.py`】。
- `task_kind` 已有注册表 `agents/task_kinds.py`，当前三项：`general`、`memory_consolidate`、`cua`；`cua` 是 `implemented=False`，调用时显式报 `task_kind_not_implemented`，不会静默回落【代码 `agents/task_kinds.py:92-135`】。
- 每个 `task_kind` 挂三样东西：**系统提示词、工具集、默认 cwd**。新增 CUA 不需要改 `create_session` / `build_worker` 的签名【代码 `agents/task_kinds.py:47-55`】。
- worker 现有工具：`Read/Write/Edit/DeleteFile/Glob/Grep`、`Shell`、`WebSearch/WebFetch`、`new_context`、`SendMessage`【代码 `agents/task_kinds.py:58-83`、`agents/worker/graph.py`】。没有浏览器工具。

### 1.2 图片通路

**【订正 2026-11-13】原文描述的是旧实现（工具返回 `image_url` 块），现实现已改为"工具只回引用、请求时还原"，并且这是端点约束决定的。**

- `Read` 读图片时**只返回一个文本引用块** `[图片] <路径> | <mime> | <n> 字节`，**不内联 base64**；单图上限 4 MB，不缩放【代码 `tools/fs.py:47`、`tools/fs.py:256-282`】。
- 请求前，`VisionAdapterMiddleware` 把最近 ≤6 张（按路径去重）引用还原成一条**独立 `HumanMessage`**，里面才是 `image_url` 块【代码 `agents/vision.py:134-191`、`agents/middleware.py:79-87`】。**不回填 `ToolMessage`**：DeepSeek Chat Completions 只允许图片出现在 user 消息，tool 角色带图直接 400【代码 `agents/vision.py:7-12`】。
- 注入只在 `wrap_model_call` 的 `request.override` 上发生，**不写回 state**；每次请求先剥离带标记的旧副本再重建【代码 `agents/middleware.py:71-83`】。
- **原结论"checkpoint 里仍然保留 base64 原图"已被推翻**：当前设计下图片从不进入 state/checkpoint。前提是 CUA 工具也遵守"只回引用"的约定（见 §5.3 缺口 3 的保留条件）。
- 实测：这套通路可被当前模型消费，模型能正确读出截图中标题（§11）。

### 1.3 上下文预算与折叠

- 预算：soft 256k / hard 320k；折叠保留最近 8 条或 40k token；窗口内单条 ToolMessage 正文超过 4000 字符会被截断【代码 `config.py:100-105`、`agents/middleware.py:162-219`】。
- `new_context` 工具本身不删消息，只置一个信号，下一轮 `before_model` 逐条 `RemoveMessage`；切点保证不拆 `tool_call` / `ToolMessage`，且保留最后一条 `HumanMessage`【代码 `tools/context.py`、`agents/context_budget.py:114-144`】。
- **关键缺口：token 估算看不见图片。** `message_tokens()` 只累计文本内容与 `tool_calls`；`_content_text()` 对内容块只取 `text` / `content` 字段，`image_url` 块两者都没有，记为 0【代码 `agents/context_budget.py:38-60`】。`truncate_oversized_tool_messages()` 也只处理文本【代码 `agents/context_budget.py:147-168`】。
- 【订正 2026-11-13】缺口仍然存在，但**影响上限已有实测边界**：图片只在请求时注入且最多 6 张，若每张按 643 token 计，估算误差上限约 **3.9k token**，不再是原文推测的"实际上下文高 20%–40%"。补齐仍然值得做，但严重性下调。

~~这条缺口对 CUA 是致命的：截图再大，折叠算法都当它不存在。~~ **【订正 2026-11-13】** 在"图片只在请求时注入、最多 6 张"的当前实现下，它不再致命，但会让预算判断偏乐观约 4k token；§5.3 给出补齐方案。

### 1.4 运行边界与生命周期

- 文件工具只允许 `workspace_root` 内绝对路径【代码 `tools/fs.py`、契约 §7.1】；越界返回 `path_outside_workspace`。
- worker 的 `TaskSpec` 含 `cwd`、`workspace_root`、`chat_id`、`task_kind`【代码 `contracts.py:240-258`】。
- ~~worker 的 recursion limit 是 40，浏览器任务 20–30 步余量不大~~ **【订正 2026-11-13】** 现为 300（router 与 worker 都是）【代码 `config.py:85-86`】。步数不再是硬约束；仍建议把单任务步数压在 20–25，理由是**成本与延迟**，不是额度。
- worker 没有 `ReplyUser`；唯一对外通道是 `SendMessage`，当前实现是**纯异步 XADD 到 router stream，立即返回**，不会阻塞等待 router 回复【代码 `tools/worker_messages.py:22-70`】。router 的回复要等用户下一句话，再由 router 调 `send_message_to_session` 注入 worker【代码 `agents/middleware.py:247-272`、`tools/sessions.py`】。
- 停止：`StopInjectionMiddleware` 注入收尾指令后拒绝新工具调用，已在执行的工具会跑完；第 3 级由 router 侧 `taskkill /F /T` 杀整棵进程树【代码 `agents/middleware.py:275-304`、`tools/sessions.py`】。
- L3 规则层目前只接在 `Shell` 里【代码 `tools/shell.py:474-484`、`tools/rules.py`】；`ToolPolicyMiddleware` 尚未实现。**CUA 不能假设会有一个全局工具策略中间件兜底**，浏览器动作的策略必须在浏览器工具内部做确定性判断。

---

## 2. 三条技术路线对比

### 2.1 定义

| 路线 | 模型每步看到什么 | 模型输出什么 | 执行层 |
|---|---|---|---|
| ① 高层语义（DOM / a11y） | 裁剪后的 DOM 或 accessibility tree，元素带 ref | `click(ref="e12")` / `type(ref="e5", text=...)` | Playwright 定位并操作 |
| ② 纯视觉（截图 + 坐标） | 一张或多张截图 | `click(x=812, y=344)` | 按坐标注入鼠标事件 |
| ③ 混合（DOM 为主、视觉兜底） | 默认 a11y snapshot；需要时加一张截图 | 默认 ref；歧义时坐标 | 两者都支持 |

### 2.2 真实差别

| 维度 | ① 语义路线 | ② 纯视觉 | ③ 混合 |
|---|---|---|---|
| 稳定性 | **高**。ref 指向具体节点，页面结构不变就不会点错；结构变了会明确报 stale ref | **低–中**。滚动、弹窗、缩放、分辨率、懒加载都会让坐标失效；视觉模型还要"猜" | **高**。视觉只处理 DOM 表达不了的场景 |
| token 成本 | **因页面而异，不能一句话概括**。实测整页快照 58–131,828 token：长文页 131,828（仅可交互行 32,330），表单页 329（160），列表应用 162（58），画布应用 626（343）【实测，§11】 | **固定成本**。实测 1280×800 截图 **643 token**，与页面内容、PNG/JPEG 压缩无关；只随分辨率变（640×400 = 201）【实测，§11】 | 取决于策略：读长文用图、点选用 ref，通常比"每步整页快照"省 |
| 开发量 | 中。需要浏览器生命周期、ref 映射、snapshot 裁剪、下载/等待 | 低–中。动作原语简单，但要处理坐标缩放、DPI、窗口尺寸 | 高。两套动作与降级逻辑都要写 |
| 对模型要求 | 文本能力即可 | 必须支持视觉；**当前模型已支持**（`GLM-5.3-Flash`，实测可读图） | 文本为主；视觉能力可用 |
| 可观测性 | 好。快照文本可存档、可 diff、可回放 | 差。只有截图，失败时难以解释"为什么点这里" | 好 |
| 典型失败 | 自定义组件没有正确 a11y 语义；iframe / shadow DOM | 坐标偏移；动画中途截图；页面滚动 | 两套逻辑的复杂度 |
| 适合场景 | 表单、列表、后台、文档站、绝大多数 Web | canvas、远程桌面、无 DOM 的客户端 | 真实生产环境 |

### 2.3 外部证据

- Playwright MCP 官方对比表直接把"snapshot vs screenshot"列为：token 低 vs 高、精确 vs 近似、确定性 vs 易变、不需要视觉模型 vs 需要【外部 Playwright MCP Snapshots 文档】。
- 第三方对 Playwright MCP 的测量：带 ref 的 snapshot 单次约 **7,300 token**；裸 accessibility tree 约 **6,856 token**【外部 Reticle 的 token 效率文档】。
- Stagehand 团队 2026-09 的 24 任务基准（8 个站点，厂商自测，**未独立复现**）：
  - Playwright MCP 默认：**97k token/任务，75% 成功，31s**；
  - Playwright MCP 调优后：105k token/任务，88% 成功，30s；
  - Stagehand v4（混合 a11y + 高层 API）：**32k token/任务，100% 成功，18s**【外部 Stagehand 博客】。
- browser-use 的做法是 DOM 蒸馏成 5–15 KB 的语义树，视觉默认开启（`use_vision=True`），但视觉只用于 DOM 表达不清时的 grounding【外部 browser-use 官方参数文档、架构拆解文章】。

> 这些数字只能看量级，不能当精确预算：站点难度、模型、重试次数、是否命中 prompt cache 都会显著改变结果。Stagehand 的基准是自家产品对比，存在厂商偏差。
>
> **【订正 2026-11-13】** 外部"snapshot 约 7.3k token"是在**裁剪过/特定页面**上的值；本机实测同一套表示在 Wikipedia 上整页为 **131,828 token**（§11）。引用外部数字时必须带上"页面类型"限定，否则会低估一个数量级。

### 2.4 为什么推荐 ③ 而不是 ① 或 ②

- **不选 ① 作为唯一路线**：真实网页里总有一小部分东西 DOM 说不清——canvas 图表、图片按钮、自定义拖拽、验证码周边。没有视觉兜底时，这些场景只能失败。
- **不选 ② 作为主路线**：坐标点击稳定性差、只覆盖当前视口、失败时无法解释。**【订正 2026-11-13】** 原文"成本、模型依赖都更差"不成立——图片成本实测固定 643 token/1280×800，且当前模型支持视觉；在长文页它比整页 a11y 快照便宜得多。
- **选 ③**：把可点、可回放的 ref 作为**动作寻址**默认；感知按页面类型选——长文/图表用截图，表单与列表用（裁剪后的）快照，二者都不是"默认全量"。

---

## 3. 参考实现怎么做

### 3.1 browser-use

**塞进上下文的东西：**

- 裁剪后的 DOM 语义树：去掉 `script/style/svg/隐藏元素`，只留可交互节点与文本，规模约 5–15 KB；
- 可交互元素带索引，模型输出"点第 N 个元素"，执行层再映射回真实节点；
- 截图：`use_vision` 默认 `True`；另有 `"auto"` 模式，只在使用时附图【外部 browser-use 参数文档】；
- 历史步骤、上次动作结果、当前 URL/标题；
- `sensitive_data`：用占位符代替真实凭据，避免密钥进 prompt【外部 browser-use API 文档】。

**怎么控制膨胀：**

- DOM 蒸馏 + 只保留可交互节点；
- `max_steps` 默认 100（可调），到步数上限强制收尾【外部 browser-use Agent Settings】；
- 视觉可关闭或按需；
- 登录类步骤交给用户在可见浏览器里完成，agent 从新的 `state` 继续【外部 browser-use 的 skill 文档】。

**对本项目的启示：** 它的"DOM 为主 + 视觉可选"就是路线 ③；`max_steps=100` 不必照抄——本项目 recursion limit 已是 300【代码 `config.py:85-86`】，但仍建议把单任务步数预算压在 20–25，理由是成本/延迟，不是额度（【订正 2026-11-13】原文按 40 推导，已失效）。

### 3.2 Anthropic Computer Use

**塞进上下文的东西：**

- **纯截图**，没有 DOM / a11y tree；
- 模型输出 `screenshot / left_click / type / key / scroll / drag` 等成员工具调用；
- 客户端把截图作为 `tool_result` 图片返回，形成"截图 → 动作 → 新截图"的循环【外部 Anthropic Computer Use 文档】。

**怎么控制膨胀：**

- 文档明确：截图历史会快速累积，**每张约 1,000–1,800 输入 token**；单次请求超过 20 张图会触发更严格的单图限制，因此要主动管理截图历史【外部 Anthropic 文档的 screenshot history 一节】。（该数字是 Claude 口径；**本机当前模型实测 643/1280×800**，见 §11。）
- 支持 batch action（一次返回多个动作），减少往返；
- 建议在隔离的虚拟机 / 容器里跑，不碰真实桌面；
- 对工具返回内容做 prompt injection 分类器扫描【外部 Anthropic 文档】。

**对本项目的启示：** 它的"每张图 1–2k token + 20 张限制"说明要管理截图历史，但**不能照搬数字**：本机实测 643/张，且图片不写 state、最多注入 6 张，估算误差上限约 3.9k（【订正 2026-11-13】原文按"40 步 × 1.5k = 60k"推导，前提已不成立）。真正需要无脑避免的是"每步整页 a11y 快照"，不是截图。

### 3.3 OpenAI computer use / Operator

OpenAI 当前文档把浏览器方向拆成两条：

1. **ComputerToolset**：模型看截图，输出鼠标/键盘动作；示例用 1024×768 视口；应用提供 driver，负责截图、执行动作、返回新截图；动作包括 `click / double_click / drag / move / scroll / keypress / type / wait / screenshot`；
2. **BrowserToolset**：不靠坐标，而是把"读页面文本 / 操作控件 / 切标签页"暴露成普通 function tools，由 driver 执行【外部 OpenAI Computer Use 指南】。

**上下文与安全控制：**

- 截图算 image input，按图片计费；文档要求"UI 状态未知时给截图，动作一小段后给新截图，图片留在内存"；
- 支持人工审批回调：提交表单、产生外部副作用前暂停，等用户确认；
- 明确提醒：页面内容可能影响模型，应使用全新浏览器 profile / 隔离桌面，限制网络，避免把凭据放进页面文本或截图【外部 OpenAI Computer Use 指南】。

**对本项目的启示：** OpenAI 自己也不是"纯视觉一条路"——它同时提供 BrowserToolset。这佐证了"浏览器当工具"比"通用 CUA"更适合生产。

### 3.4 Playwright MCP

**塞进上下文的东西：**

- accessibility snapshot（YAML 风格），每个节点带 `ref=e12`；
- 大多数动作执行后**自动返回一张新 snapshot**；
- 支持 `browser_snapshot` 的 `target` / `depth` 裁剪，`browser_find` 只返回匹配子树；
- ref 失效时明确报错："Ref not found in the current page snapshot. Try capturing new snapshot."【外部 Playwright MCP Snapshots 文档】。

**怎么控制膨胀：**

- 文本快照，不用图；
- 按子树 / 深度裁剪；
- `browser_find` 代替整页快照；
- 可全局关掉"每步自动附快照"（`--snapshot-mode=none`），让模型显式取。

**对本项目的启示：** 这套 `ref + 快照 + stale ref 报错 + find` 的交互，是最适合照抄的部分；它比"让模型输出 CSS selector"稳，也比"输出坐标"便宜。

### 3.5 Stagehand / 社区做法

- Stagehand 的核心是**混合 a11y 快照 + 三个高层 API**：`observe()`（找可操作元素）、`act()`（执行一个高层动作）、`extract()`（结构化抽取）；把"看整棵树"变成"问一个具体问题"，官方声称比 Playwright MCP 少约 80% token【外部 Stagehand 博客，厂商自报】。
- 社区共识基本一致：
  - 能用 DOM/a11y 就不用视觉；
  - 视觉只做兜底；
  - 快照要裁剪、要能按需取；
  - 登录/验证码交给人；
  - prompt injection 把页面内容当数据。

> **注（2026-11-13 实测）**：上面第 1 条只在"**动作寻址**"上无条件成立；"**感知**"上 DOM/a11y 并不必然更省 token——长文页整页快照 131,828 token，单屏截图 643。引用社区共识时不要把"省 token"当成 DOM 的固有优势（§11）。

### 3.6 参考实现横向对照

| 实现 | 主感知 | 视觉角色 | 动作寻址 | 上下文控制手法 |
|---|---|---|---|---|
| browser-use | DOM 语义树 | 默认开，可 auto/off | 元素索引 + 坐标兜底 | DOM 蒸馏、max_steps、可关视觉 |
| Anthropic CU | 截图 | 唯一感知 | 坐标 | 截图历史上限、batch、隔离 VM |
| OpenAI CUA | 截图 | 唯一感知（ComputerToolset） | 坐标 | 审批、隔离 profile、截图按需 |
| OpenAI BrowserToolset | 页面文本/控件 | 无 | 高层 function tool | 工具粒度控制 |
| Playwright MCP | a11y snapshot | 可选（vision mode） | ref | depth/subtree/find、可关自动快照 |
| Stagehand | 混合 a11y | 可选 | 高层 act/observe | 高层 API、混合快照 |

**结论：没有一家把"每步都发全页截图"当默认最佳实践。** 我们的推荐与主流一致。

---

## 4. 接进现有架构：`cua` worker 长什么样

### 4.1 总体形态

```
router.create_session(task_kind="cua")
  → workers/<task_id>/task.json
  → worker_runner.py（同一个入口，不新增进程类型）
  → task_kinds.resolve("cua") 返回专用工具集与 cwd
  → Playwright 在 worker 进程内惰性启动 Chromium
  → 每步：snapshot → 模型决策 → 浏览器动作 → 新 snapshot
  → 中途 SendMessage(notify/ask) 向 router 汇报
  → 结束：worker_result（文本结论 + 产物路径）
```

不新增进程类型、不新增 Redis key、不改 router 的消息语义——这是最低成本接法。

### 4.2 工具集（v1 建议 9 个）

所有工具遵守现有约定：`@tool`、中文 docstring、失败不抛异常、返回 dict、路径限制在 workspace 内。

| 工具 | 入参 | 返回 | 说明 |
|---|---|---|---|
| `BrowserOpen` | `url` | `{ok, url, title, snapshot?}` | 首次调用时惰性启动浏览器；只允许 http/https |
| `BrowserSnapshot` | `depth=None, target=None, max_chars=20000` | `{snapshot, truncated, ref_count, artifact?}` | 裁剪后的 a11y 树；超限落盘 + 返回摘要 |
| `BrowserFind` | `text` 或 `regex` | `{matches:[{ref, path, text}]}` | 大页面只取匹配子树，避免整页快照 |
| `BrowserClick` | `target`（ref 或 selector）, `button="left"` | `{ok, url?, title?, snapshot?}` | ref 失效返回 `stale_ref` + hint |
| `BrowserType` | `target`, `text`, `submit=False` | `{ok, snapshot?}` | `submit=True` 才按回车；不自动提交表单 |
| `BrowserScroll` | `direction`, `amount=1` | `{ok, snapshot?}` | 滚动后必须重新快照 |
| `BrowserWait` | `condition`（`load`/`selector`/`text`）, `value`, `timeout_ms=10000` | `{ok, elapsed_ms}` | 显式等待，替代 sleep |
| `BrowserExtract` | `selector=None`, `max_chars=20000` | `{text, truncated, artifact?}` | 读正文/表格；长文落盘 |
| `BrowserDownload` | `target`, `save_dir=None` | `{ok, path, bytes}` | 必须落在 `workspace_root/cua/<task_id>/downloads/` |

可选（v1.1 再加）：`BrowserScreenshot`、`BrowserBack`、`BrowserTabs`。

**为什么截图单独一个工具**：默认不产生图片，只有模型明确需要视觉时才调用；**第一道闸的作用是控制"覆盖范围与精度匹配"，不是省 token**（实测单张 643 token，见 §11）。

### 4.3 cwd 与产物目录

建议：

```
workspace_root/
  cua/
    <task_id>/
      downloads/          # BrowserDownload 默认落点
      notes/              # 可选：模型写任务中间状态
data_dir/
  browser-profile/        # 专用 Chromium 持久化 profile（不放 workspace）
  tool_outputs/<trace_id>/
    <tool_call_id>-snapshot.md
    <tool_call_id>-page.txt
    <tool_call_id>-shot.png
  workers/<task_id>/result.json
  logs/worker-<task_id>.log
```

- 浏览器 profile 放 `data_dir`，不放 `workspace_root`：避免模型通过文件工具直接读到 cookie 数据库；
- 截图/快照落 `tool_outputs`，与现有 trace 关联规则一致（`ToolContextMiddleware` 已经保证 `current_tool_call_id` 可用）；
- 下载目录固定在任务 cwd 下，`BrowserDownload` 内部做 `resolve_in_workspace` 校验。

### 4.4 装配点

`agents/task_kinds.py` 的 `cua` 注册项从：

```python
TaskKindSpec(kind="cua", ..., implemented=False)
```

改为（示意，不在本次改动范围）：

```python
TaskKindSpec(
    kind="cua",
    system_prompt=cua_prompt,        # 需 agents/prompt.py 归属会话新增
    build_tools=_cua_tools,          # 新建 tools/browser.py 的工厂
    default_cwd=lambda s: s.workspace_root / "cua",
)
```

注意两点：

1. **系统提示词归 `agents/prompt.py`**，另一个会话在维护，本文只提出"需要一个 cua 版 prompt"，不提供正文、不改文件；
2. 新增配置项（`browser_headless`、`browser_profile_dir`、`cua_max_steps`、`cua_screenshot_max_inline`、`cua_download_max_mb`、`cua_allowed_domains` 等）属于 `config.py` 冻结文件，必须走契约 §11 的变更流程，不能由 CUA 实现方直接改。

### 4.5 与 router 的交互

现有 `SendMessage` 是异步的，只有 `notify` / `ask` 两种语义【代码 `tools/worker_messages.py`】。CUA 任务的特点是"中途经常需要人"：

| 场景 | 用什么 | 现状与限制 |
|---|---|---|
| 进度汇报（"已到搜索结果页，准备筛选"） | `SendMessage(kind="notify")` | 可用；建议节流：每 3–5 步或每次页面跳转才发一次，避免刷屏 |
| 需要用户决策（付款确认、登录、验证码、选哪个结果） | `SendMessage(kind="ask")` | **只能异步发出**，不会阻塞等待 router 回复；用户回话后，router 再 `send_message_to_session` 注入。往返可能几分钟 |
| 最终结果 | worker 结束时 `worker_result` | 可用；结果里给产物路径 |

因此 v1 的交互原则：

- **不把"ask"设计成同步阻塞**。worker 发完 `ask` 后，如果当前动作不可逆，就**停下并结束本任务**，把问题留给 router；不要一边等一边继续点。
- 可逆动作（滚动、打开新页面、读文本）不需要问。
- 不可逆动作（提交订单、发送消息、删除内容、付款）**必须先得到明确同意**；v1 最保守做法是直接不执行，返回 `need_user_confirm`。

### 4.6 失败恢复

| 失败 | 处理 | 重试预算 |
|---|---|---|
| `stale_ref`（页面变了） | 重新 `BrowserSnapshot`，让模型按新 ref 重规划 | 2 次 |
| 元素不可见 / 被遮挡 | `scroll_into_view` → 重新快照 → 再点 | 1 次 |
| 导航超时 | 读当前 URL/title，判断是否已到达；未到达重试一次 | 1 次 |
| 意外弹窗 / cookie 横幅 | 快照里识别 dialog，优先关闭；无法判断则 `ask` | 1 次 |
| 登录墙 / 验证码 | **停止**，`SendMessage(ask)`，交给用户在可见浏览器里完成 | 0（不自动尝试） |
| 下载失败 | 等待 10s，检查下载目录，仍无则报错 | 1 次 |
| 浏览器崩溃 | 重启一次，恢复最后 URL，重新快照 | 1 次 |
| 步数接近上限 | 主动收尾：已完成什么、卡在哪、产物在哪 | — |

原则与现有架构一致：**不自动重派、不无限重试、失败也回传 `worker_result`**。

---

## 5. 上下文策略：现有折叠扛不扛得住

### 5.1 先算一笔账

**【订正 2026-11-13】原算式的两个单价都错了：snapshot 不是稳定 5k，图片也不是 1.3k。按实测值重算【实测，§11】：**

假设一个中等复杂度任务 20 步，且每步都取一次感知：

- 长文页整页 a11y snapshot：**131,828 token/页** → 第一步就超过 soft 256k 的一半；20 步根本不可行（必须裁剪或改用截图）。
- 表单/列表应用页整页快照：**162–329 token/页** → 20 步约 **3k–7k token**，很便宜。
- 每步一张 1280×800 截图：**643 token/张** → 20 步约 **12.9k token**；即使历史全留，也只占 soft 预算的 5%。
- 工具调用参数、模型输出、系统提示：约 10k–20k。
- 请求历史逐轮累积，实际"请求输入总量"通常是最终上下文的好几倍；命中 prompt cache 后计费会降，但**上下文窗口占用不会降**。

结论：**"文本快照更省、截图更贵"只在短页面成立；长文页相反。真正的风险不是截图太贵，而是"不裁剪的整页快照"一步就把预算吃光。**

### 5.2 现有折叠能做什么

能做的：

- 按消息条数 / 文本 token 折叠旧消息；
- 不拆 `tool_call` / `ToolMessage`；
- 保留最后一条 `HumanMessage`；
- 硬预算时截断长文本工具输出；
- 手动 `new_context` 可以指定更激进的保留窗口。

**这些对 CUA 仍然有用**，因为每步的 snapshot 是文本，折叠能删掉旧的页面快照。

### 5.3 现有折叠的缺口（原文三条，2026-11-13 复核后剩两条）

#### 缺口 1：估算器看不见图片

`estimate_tokens()` 对 `image_url` 块计 0【代码 `agents/context_budget.py:38-60`】。**【订正 2026-11-13】影响量级按实测重算：**

- 当前实现最多注入了 **6 张**（`agents/vision.py:131`），每张 643 token → 低估上限约 **3.9k token**，不是原文的"40 步 × 1.5k = 60k"；
- 因此不会出现"估算 280k、实际超 320k"的失控（除非 CUA 自己把图片写进 state）；
- 但仍建议补：在 `message_tokens()` 里按 `643/1280×800` 或 `宽×高/1600` 估图片块，并在 `_content_text()` 识别 `image_url`。这是**准确性**改进，不是上线阻塞项。

#### 缺口 2：硬预算截断不处理图片

`truncate_oversized_tool_messages()` 只把超长文本截断【代码 `agents/context_budget.py:147-168`】。**【订正 2026-11-13】** 当前 `Read` 不在 ToolMessage 里放图片块，所以现状触发不到；但 CUA 的 `BrowserScreenshot` 如果直接返回 `image_url` 块就会踩中。

**仍建议补**：硬预算时把旧 ToolMessage 里的图片块替换成文本 stub（"截图已落盘：<path>"），只保留最近 1–2 张。

#### ~~缺口 3：checkpoint 会把 base64 图片存很多份~~ **【订正 2026-11-13】已被现有设计规避**

原文依据的是旧实现（工具直接返回 `image_url`）。现实现中：

- `Read` 只回文本引用，不内联 base64【代码 `tools/fs.py:256-282`】；
- 图片在 `wrap_model_call` 里以独立 user 消息临时追加，`request.override` **不写回 state**【代码 `agents/middleware.py:71-83`；`agents/vision.py:134-191`】；
- 因此 checkpoint 里没有 base64，原文"20 步 × 5 张 × 1.5 MB ≈ 150 MB/任务"的推算**在当前实现下不成立**。

**保留条件**：CUA 工具必须遵守同一约定——只返回路径/引用，不把 base64 或 `image_url` 块塞进 ToolMessage。一旦违反，这条风险立即恢复。设计约束因此从"必须新增基础设施"降级为"工具实现纪律"。

### 5.4 建议的 CUA 专用策略（不改现有 middleware 语义）

按优先级：

1. **截图按需、默认只落盘**：`BrowserScreenshot` 返回 `{path, bytes, width, height}`；模型要看图时再显式请求（独立工具或 `Read` 读该 png）。**【订正 2026-11-13】** 理由从"图太贵"改为"图只覆盖视口、且多图会累积占窗口"——实测单张仅 643 token，贵不是问题。
2. **snapshot 先裁剪再入上下文**：只保留 role/name/state/ref 与可见文本；超过 20k 字符落盘，返回"前 N 个可交互元素 + 文件路径 + 用 `BrowserFind` 缩小范围"。**【订正 2026-11-13】** 这条从"优化项"升为**必须项**：Wikipedia 整页 131,828 token，不裁剪一步就超预算（§11）。
3. **每步只保留最新快照**：旧的页面快照在下一轮折叠时优先删除；页面状态以最新快照为准，不靠模型记忆。
4. **CUA 单独一组预算**（建议值，需配置化）：
   - `cua_context_soft_tokens = 120_000`；
   - `cua_keep_recent_messages = 6`；
   - `cua_keep_recent_tokens = 20_000`；
   - `cua_max_inline_screenshots = 1`；
   - `cua_screenshot_token_est = 650`（实测 643/1280×800，留 ~1% 余量）。
   理由：浏览器任务天然短（20–25 步），不需要 256k 的大窗口；早折叠反而能降低单次请求成本。
5. **折叠后必须重新快照**：折叠会删掉历史页面文本，模型不能凭记忆点元素；prompt 里要写死"折叠后先 `BrowserSnapshot` 再动作"。
6. **任务状态外置**：让模型在关键节点写 `notes/progress.md`（用现有 `Write`），折叠后读回来。这比让模型记住 20 步前的页面可靠。
7. **checkpoint 保留期缩短**：CUA 任务的截图含用户隐私，建议任务完成后 1–7 天内删除 thread，而不是复用 general 的 7 天策略。

### 5.5 结论

**现有 `new_context` + 折叠"够用，但配置要按页面类型调"：**

- 文本快照：机制能扛；**但整页不裁剪的长文页快照一步就能顶到预算**（Wikipedia 131,828 token），所以裁剪/`find` 是前置条件，不是优化。
- 截图：**【订正 2026-11-13】** 不再"扛不住"——图片不写 state、最多注入 6 张、单张 643 token；风险从"存储/上下文爆炸"降为"估算偏差 ~4k"。
- 结论：不需要新基础设施；只要坚持"工具只回引用/路径 + snapshot 必裁剪 + 补图片估值"，现有 middleware 足够。

---

## 6. 安全与边界

### 6.1 登录态与凭据

| 方案 | 评价 | 建议 |
|---|---|---|
| 直接接管用户日常 Chrome（含所有 cookie） | 风险最高：个人数据、支付、邮箱全暴露；Chrome 还可能被用户同时占用 | **不做** |
| 复制用户 Chrome profile 到 agent 目录 | 仍然复制了全部凭据；profile 加密与版本兼容也麻烦 | **不做** |
| 专用持久化 profile（`data_dir/browser-profile`），首次由用户在可见窗口手动登录 | 凭据只存在本地 profile；agent 只操作，不读取 cookie | **v1 采用** |
| 工具内保存账号密码 / 从 `.env` 取密码自动填 | 密钥会进 prompt 或日志，违反现有"工具不得读 `.env`"约束 | **不做** |
| `sensitive_data` 占位符（browser-use 式） | 需要额外的占位符替换层，v1 复杂度偏高 | v2 再评估 |

v1 的登录流程：

1. 检测到登录墙 → 停止 → `SendMessage(ask)`；
2. router 告知用户"请在弹出的浏览器窗口里登录"；
3. 用户手动登录后，worker 重新快照继续；
4. 全程不记录账号、密码、cookie、token。

### 6.2 能不能碰用户已登录的浏览器

**默认不能。** 原因：

- 无法区分"用户希望 agent 用的登录态"和"用户不想被碰的登录态"；
- 同 profile 并发会锁库、串会话；
- 截图可能把邮箱、聊天、银行页面带进模型上下文。

例外：用户在任务里明确说"用我已经登录的某站点"，并且该站点在允许列表内。即使如此，也建议用专用 profile + 用户手动登录，而不是接管日常浏览器。

### 6.3 下载落哪

- 固定 `workspace_root/cua/<task_id>/downloads/`；
- 文件名做净化（去掉路径分隔符、`..`、控制字符）；
- 单文件上限建议 20 MB，单任务上限 100 MB；
- **下载后不自动打开、不自动执行**；
- 返回绝对路径 + 字节数，由 router 决定要不要发给用户。

### 6.4 任意站点的风险

| 风险 | 最小可行约束 |
|---|---|
| 误点支付 / 下单 / 转账 | 域名 denylist（银行、支付、券商、加密钱包）+ 动作关键词 denylist（pay/checkout/confirm order/transfer）；命中直接拒绝，不询问 |
| 误发消息 / 误删内容 | 提交类动作默认 `submit=False`；不可逆动作一律 `need_user_confirm`，v1 不执行 |
| 钓鱼 / 恶意页面 | 只允许 http/https；`file://`、`chrome://`、`javascript:` 一律拒绝；可选域名 allowlist |
| prompt injection（页面里写"忽略之前的指令"） | 页面内容一律当数据；浏览器工具不解析页面里的"指令"；固定工具集，不因页面内容新增能力；系统提示里沿用现有 `WEB_CONTENT_IS_DATA` 原则 |
| 截屏泄露隐私 | 截图只落 `tool_outputs`；任务结束按保留期清理；默认不把截图发飞书 |
| 多任务并发抢同一个 profile | v1 **串行执行 CUA 任务**（全局锁或单实例检测），第二个任务排队或拒绝 |
| 进程残留 | 复用现有三级终止；worker 退出时 `finally` 关闭浏览器；`taskkill /F /T` 能带走 Chromium 子进程 |

### 6.5 审计

建议每个 CUA 任务写一份 `workers/<task_id>/browser-audit.jsonl`，每行：

```json
{"ts":"...","step":7,"action":"click","target":"e42","url":"...","title":"...","result":"ok","shot":"tool_outputs/.../shot.png"}
```

只记动作与 URL，不记输入框内容（避免密码/隐私进日志）。这份日志是"事后解释 agent 干了什么"的唯一依据，比截图更可控。

---

## 7. 成本量级

### 7.1 单任务 token

以一个 20 步任务估算。**【订正 2026-11-13】按实测单价重算，并区分页面类型：**

| 组成 | 实测/估算单价 | 20 步合计 |
|---|---|---|
| a11y snapshot（长文页，未裁剪） | 131,828/页【实测】 | 不可行，必须裁剪或改截图 |
| a11y snapshot（表单/列表页，未裁剪） | 162–329/页【实测】 | 约 3k–7k |
| a11y snapshot（仅可交互行） | 58–32,330/页【实测】 | 1k–646k（长文页仍不可接受） |
| 截图（1280×800，每步一张） | **643/张**【实测】 | **12.9k** |
| 模型输出 + 工具参数 | 0.3k–0.8k【估算】 | 6k–16k |
| 系统提示 + 历史 | — | 10k–20k |

**合理组合下的量级**：短页面任务用"裁剪快照"约 **20k–40k**；长文阅读任务用"按屏截图"约 **25k–50k**；只有"每步整页快照"才会冲到 **100k+**——那是最该避免的用法。

实际计费还要看：

- 请求历史逐轮累积，输入 token 的"总量"通常是最终上下文的 3–8 倍；
- 命中 prompt cache 可显著降低计费，但不降低窗口占用；
- 失败重试按 1.3–1.5 倍系数放。

外部实测可作锚点【外部 Stagehand 基准，厂商自测】：

- Stagehand 混合方案：**32k token/任务，18s，24/24**；
- Playwright MCP 默认：**97k token/任务，31s，18/24**；
- Playwright MCP 调优：105k token/任务，30s，21/24。

### 7.2 跟纯 API 方案比

纯 API 方案指：`WebFetch` 抓页面 + 站点公开 API + 本地解析，不驱动浏览器。

| 方案 | 典型 token | 典型耗时 | 成功率 | 相对成本 |
|---|---|---|---|---|
| 纯 API / WebFetch | 10k–30k | 2–10s | 高（确定性） | 1× |
| DOM/a11y 浏览器 agent（**不裁剪**） | 100k–600k | 20–60s | 中–高 | **约 5–20×** |
| 混合（裁剪快照 + 按需截图） | 25k–60k | 30–80s | 中–高 | **约 2–4×** |
| 每步截图的纯视觉 CUA | 20k–50k（图便宜，但步数多） | 60–180s | 中 | **约 2–5×** |

**【订正 2026-11-13】** 原表假设"截图贵、快照便宜"，把纯视觉写成 150k–400k token。按实测（643/张），纯视觉的成本主要来自**步数和延迟**，不是图片 token；而"不裁剪的 a11y"才是最贵的一列。按常见低价模型 1–4 元/百万 input token 估算，单任务大约 **0.1–1.2 元**【估算，单价以实际账单为准】。

**真正的成本不是钱，是延迟和失败率**：一次浏览器任务 30–60 秒，失败重跑一次就翻倍；而纯 API 通常 2–10 秒且确定性高。

### 7.3 什么时候浏览器方案才划算

满足以下任意两条，浏览器方案才比"让用户自己点"或"纯 API"更划算：

1. 目标站点**没有公开 API**，或 API 需要企业资质；
2. 任务**必须带登录态**，且登录后只能通过页面操作；
3. 同一流程**每周重复 ≥3 次**；
4. 人工操作一次需要 ≥2 分钟，且步骤稳定；
5. 失败可接受（不是付款、不是发消息、不是删数据）。

如果只是"读一个公开页面"，用 `WebFetch`；如果站点有 API，优先接 API。**浏览器不是默认答案，是最后手段。**

---

## 8. 分阶段建议

### 8.1 P0：可行性验证（不写正式代码）

目标：用最小脚本回答"能不能做"，产出一页实测记录。

| 实验 | 通过标准 | 2026-11-13 状态 |
|---|---|---|
| 模型收图 | 一张 1024×768 截图，真实请求不 400，usage 有图片 token | **通过**：`usage.prompt_tokens=696`，模型正确读出标题（§11） |
| ToolMessage 带图 | 明确知道合法/不合法；不合法则确定替代注入方式 | **已定论**：该端点 tool 角色带图 400，现实现改为"工具回引用 + 请求时注入 user 消息"（§1.2） |
| Playwright 启动 | conda `feishu` 环境能装、能起 Chromium、能拿到 a11y snapshot | **通过**：Playwright 1.65.1 + 独立 Chromium 158，headless 正常（§11.1 路径） |
| snapshot 体积 | ~~5 个目标站点，单页 token 中位数 ≤8k~~ | **未通过/需改标准**：4 类页面实测 58–131,828 token，中位数无意义；改为"单页裁剪后 ≤8k，否则强制 `find`/截图"（§11） |
| 端到端小任务 | 3 个任务（读列表 / 填表单不提交 / 下载文件），至少 2 个成功 | 未验证 |
| 停止与清理 | `StopCommand` 后浏览器进程不残留 | 未验证（浏览器内核层面已验证可正常关闭） |

P0 任何一项失败，先修正路线再谈 v1。

### 8.2 v1：能用的最小范围

**做：**

- 9 个浏览器工具（§4.2）；
- 专用持久化 profile + 用户手动登录；
- a11y snapshot 为主，截图按需、默认只落盘；
- 域名 denylist + 不可逆动作拒绝；
- 下载限制在任务目录；
- 进度 `notify`、求助 `ask`、结束 `worker_result`；
- CUA 专用上下文预算 + 图片估值 + 图片 stub；
- 任务级审计 JSONL；
- CUA 任务串行执行。

**明确不做：**

- 桌面控制（鼠标键盘操作非浏览器窗口）；
- 接管用户日常 Chrome；
- 保存账号密码 / 自动登录 / 读 cookie；
- 破解验证码；
- 支付、下单、转账、删除账号等不可逆动作；
- 多标签页并行、多任务并行；
- 通用任意站点承诺（v1 只承诺"目标站点清单内可用"）；
- 纯视觉坐标点击作为主路径。

**验收标准（建议）：**

1. 5 个真实站点、10 个任务，成功率 ≥70%；
2. 单任务 p95 ≤3 分钟；
3. 单任务 token ≤150k（混合模式）；
4. 无越界写入、无凭据进日志、无截图进飞书；
5. `StopCommand` 三级生效，浏览器无残留；
6. 折叠后仍能继续任务（至少一次 20 步以上的长任务验证）。

### 8.3 v2 再考虑

- 视觉兜底自动化（点击失败 → 自动附图重试）；
- `BrowserTabs`、多标签；
- 结构化抽取（`extract` schema）；
- 站点级"技能"（固定流程脚本化，减少模型步数）；
- 凭据占位符（sensitive_data 式）；
- 域名 allowlist 与更细的审批 UI。

### 8.4 投入量级（粗估）

- P0：小脚本 + 实测，不进入主干；
- v1：1 个新工具模块 + 1 个 task_kind 注册项 + 少量配置 + 上下文补丁 + 测试；
- 不新增服务、不新增 Redis key、不新增进程类型。

真正的工作量在**站点适配与失败处理**，不在"调通一次点击"。

---

## 9. 风险清单与未验证项

### 9.1 未验证 / 最新状态（2026-11-13 复核）

| 项 | 为什么重要 | 状态 |
|---|---|---|
| ~~当前模型是否真支持视觉~~ | 决定纯视觉/截图兜底是否可用 | **已验证支持**：`config.py:61` 默认 True，`.env` true；实测可读图（§11） |
| ~~`ToolMessage` 能否带 `image_url`~~ | 决定截图注入方式 | **已验证不合法**（该端点 tool 角色带图 400），已改为 user 消息注入（§1.2） |
| snapshot 在目标站点的真实体积 | 决定预算与是否先裁剪 | **部分验证**：4 类页面已测（§11）；目标站点清单未定，仍需逐站抽测 |
| 超长上下文 + 图片混排的稳定性 | 可能是隐性 400 或截断 | **部分验证**：单图 1280×800 正常；**整页长图（1280×48985）会因单边 >8192px 被拒**【代码 `agents/vision.py:10`；§11】 |
| Playwright 在 Windows + conda 环境的启动耗时 | 影响任务延迟 | 未验证（本次只记录功能可用） |
| Chromium 下载与磁盘占用 | 首次安装体验 | **已验证**：独立 158 共 775,906,827 字节（约 740 MiB） |

### 9.2 已知风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| ~~模型视觉关闭（当前配置）~~ **【订正 2026-11-13】不成立**，现为 True 且实测可用 | — | 不再需要缓解；保留视觉降级中间件以兼容纯文本模型 |
| 上下文估算忽略图片 | 预算偏乐观约 4k token | 补图片估值与 stub（准确性改进，非阻塞） |
| ~~checkpoint 存 base64 截图~~ **【订正 2026-11-13】现设计不写 state** | 仅当 CUA 工具自行内联图片时恢复 | 工具只回路径/引用；`Read` 已有范例 |
| ~~recursion limit 40~~ **【订正 2026-11-13】现为 300** | 步数不是瓶颈 | 仍建议步数预算 20–25，理由是成本/延迟 |
| `SendMessage(ask)` 不阻塞 | 无法"问完立刻继续" | 不可逆动作直接停；可逆动作不问 |
| 站点反爬 / 风控 | 任务随机失败 | 串行、降速、目标站点清单化；不承诺任意站点 |
| prompt injection | 页面诱导 agent 越权 | 页面当数据、固定工具集、危险动作拒绝 |
| 用户隐私进截图 | 泄露 | 专用 profile、截图保留期、默认不发飞书 |
| 多实例抢 profile | 会话串号 | 单实例检测 + 串行 |

---

## 10. 最终建议（决策用）

1. **做，但只做"浏览器当工具"，不做"通用视觉 CUA"。**
2. **路线选 ③ 混合**：ref 用于动作寻址，感知按页面类型选（长文/图表用截图，表单/列表用裁剪快照）。**【订正 2026-11-13】选它的首要理由是定位确定、可回放，不是"更省 token"**——实测长文页整页快照 131,828 token，反而远贵于单屏截图 643。
3. **v1 范围收窄**：9 个工具、专用 profile、目标站点清单、串行执行、不可逆动作拒绝。
4. **P0 的"模型收图 / ToolMessage 带图"已完成**（2026-11-13）：模型支持视觉、端点要求图片走 user 消息。剩余待验：目标站点快照体积、端到端 3 个小任务、停止清理。
5. **上线前补上下文能力**：图片 token 估值、旧图片 stub、CUA 专用预算。其中"图片估值"从必改降为准确性改进（误差上限约 4k）；**snapshot 裁剪升为必改**。
6. **如果目标只是读公开网页，直接用 `WebFetch`**；如果目标站点有 API，优先接 API。浏览器方案只在"必须登录 + 必须点击 + 没有 API + 重复发生"时才有性价比。

---

## 11. 实测附录：ref 快照 vs 截图，谁更省 token（2026-11-13）

### 11.1 方法（可复现）

- **浏览器**：独立 Chrome for Testing **158.0.8127.18**（`C:\Users\34897\Documents\Trae-CN\temp_20261108_06\cua-browsers\chromium-1254\chrome-win64\chrome.exe`），headless，视口 **1280×800**；**不使用系统 Chrome、不使用 `channel`**。
- **模型**：2026-11-17 起 `.env` 为直连 —— `LLM_MODEL=deepseek-flash`，`LLM_BASE_URL=https://api.deepseek.com/v1`，`LLM_SUPPORTS_VISION=true`（本文测量时用的是 `GLM-5.3-Flash` 经本地网关，网关已移除）。
- **表示 A（ref 式）**：`page.locator("body").aria_snapshot()` 全量输出，再给可交互 role 追加 `[ref=eN]`（共 2,242/13/4/26 个 ref）。
- **表示 B（截图）**：`page.screenshot()` 视口 PNG，作为 data URI 发送。
- **token 读数**：OpenAI 兼容端点的 `usage.prompt_tokens`。基线为"同一 system + 用户发 `ok`"= **44**；A 的基线为"system + `页面快照（aria）：` 前缀"= **50**。表格里的 token 均为**差值**，不是字符估算。
- **脚本与产物**（仓库外，未改业务代码）：
  - `C:\Users\34897\Documents\Trae-CN\temp_20261108_06\cua-token-bench\measure.py` → `out\result.json`
  - `...\probe2.py` → `out\result2.json`（ref-only、JPEG、视觉抽检）
  - `...\probe3.py` → `out\result3.json`（整页截图尺寸与 token）
  - 每页快照 `out\<page>-aria.txt`、截图 `out\<page>.png`

### 11.2 对照表

| 页面 | 类型 | 整页快照 A（token） | 仅可交互行（token） | 截图 B（1280×800，token） | 谁更省 |
|---|---|---|---|---|---|
| `en.wikipedia.org/wiki/Google_Chrome` | 信息密集文档 | **131,828**（462,550 字符） | 32,330（104,468 字符） | **643** | **截图**，差 205× |
| `httpbin.org/forms/post` | 表单 | 329（1,033 字符） | 160 | 643 | **快照**，约 2× |
| `demo.playwright.dev/todomvc/` | 列表型应用 | 162（476 字符） | 58 | 643 | **快照**，约 4× |
| `excalidraw.com` | 画布应用 | 626（1,504 字符） | 343 | 643 | 接近，快照略省 |

> B 的 643 是四个页面**完全相同**的：图片 token 只由分辨率决定，与页面内容无关。同尺寸 PNG（178 KB）与 JPEG q80（163 KB）都是 **643**；640×400 是 **201**。

### 11.3 图片 token 是怎么算出来的

- 直接读 `usage.prompt_tokens` 差值，不套公式；观测点：
  - 1280×800 → **643**
  - 640×400 → **201**
  - 压缩格式/体积（PNG vs JPEG）→ 不变
- 面积比是 4.0，token 比是 3.2，说明**不是严格的"像素数 ÷ 固定值"**；代码注释记录的端点规则是"服务端把大图缩到 ~1300px 等效像素再计费，单图 token 上限 1024"【代码 `agents/vision.py:10-11`】。643 落在该上限内，与"1280×800 不必再缩"一致；但**精确的 tile/patch 公式未验证**。工程结论两条：只跟分辨率相关；当前尺寸按 643 预留。
- **整页截图不可直接发送**：端点要求单边 ≤8192px【代码 `agents/vision.py:10`】。Wikipedia `full_page=True` 是 1280×48985，请求被拒（`unsupported image`）；按项目注入规则缩到最长边 2048 后变成 **54×2048**（宽 54 像素，文字不可读），token 323。所以"一张图看完整页"在实践中不成立，截图必须按视口分屏。

### 11.4 分界线：什么样的页面图便宜、什么样的快照便宜

以 1280×800 单屏 **643 token** 为基准：

- **快照更便宜**：整页文本 ≲ 2,000 字符，或可交互节点 ≲ 30 个的表单/小应用（实测 160–626 token）。这类页面一屏能看全，快照还附带 ref，何乐不为。
- **截图更便宜**：长文/文档/新闻/表格页。整页快照与文本量线性增长，一页就 13 万 token；按屏截图则是 643/屏。要看完整页，Wikipedia 需要约 61 屏 × 643 ≈ **39,223 token**，仍比整页快照（131,828）便宜，但已略高于"仅可交互行"（32,330）——**此时快照的 ref 定位价值开始值得这点差价**。
- **都不要全量**：无论选哪种，默认都应"裁剪/按屏"，而不是"每步整页"。

### 11.5 token 低 ≠ 好：并列维度

| 维度 | ref 快照 A | 截图 B |
|---|---|---|
| 定位精度 | **高**：ref 指向具体节点，结构变更有显式 `stale_ref` | 低：坐标随滚动/弹窗/DPI 漂移，需视觉再 grounding |
| 可操作性 | 直接 `click(ref)`；但 canvas / 自定义组件可能没有可交互节点 | 任何可见内容都能"点"，但点得准不准取决于模型视觉精度 |
| 信息完整性 | 整页文本都在，适合表单/列表/结构化抽取 | 只覆盖当前视口；长页要分屏，拼接成本 = 屏数 × 643 |
| 分辨率损失 | 无（文本） | 有：1280×800 尚可；整页缩到 2048 长边后宽仅 54px，文字不可读（实测） |
| 可解释/回放 | 强：快照可存档、diff | 弱：只有像素，失败难解释 |
| 稳定性风险 | 自定义组件 a11y 语义缺失、iframe/shadow DOM | 动画中途截图、懒加载未完成、反爬对 headless 更敏感 |

**因此结论不是"以后都用图"**：token 账上截图赢长文页，但点选仍应走 ref；图适合"读"，ref 适合"点"，两者按任务阶段切换才是 ③ 的本意。

---

## 参考来源

> 访问日期：2026-11-09。厂商基准未独立复现，仅用于量级参考。

- [Anthropic Computer use tool 文档](https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool)
- [Anthropic Computer use tool 文档（德语版，含截图 token 与 20 张限制说明）](https://platform.claude.com/docs/de/agents-and-tools/tool-use/computer-use-tool)
- [OpenAI Computer use 指南（ComputerToolset / BrowserToolset）](https://platform.openai.com/docs/guides/tools-computer-use)
- [Playwright MCP Snapshots 文档](https://playwright.dev/mcp/snapshots)
- [Playwright MCP Introduction](https://playwright.dev/mcp/introduction)
- [Stagehand：Why Playwright MCP uses so many tokens（2026-09）](https://www.stagehand.dev/blog/playwright-mcp-token-usage)
- [browser-use Agent Settings](https://docs.browser-use.com/customize/agent-settings)
- [browser-use All Parameters](https://docs.browser-use.com/open-source/customize/agent/all-parameters)
- [browser-use 架构拆解：DOM Tree Distillation, Vision Grounding](https://dev.to/ifnodoraemon/under-the-hood-of-browser-use-100k-stars-dom-tree-distillation-vision-grounding-and-5363)
- [Reticle：token efficiency 测量（Playwright MCP 约 7.3k token/snapshot）](https://raw.githubusercontent.com/reticlehq/reticle/HEAD/docs/token-efficiency.md)
- [WebVoyager 等 agent 对比（第三方汇总，数据口径不一，仅作参考）](https://datarekha.com/blog/operator-vs-claude-computer-use-vs-mariner/)
- [WebSight: A Vision-First Architecture for Robust Web Agents（arXiv 2508.16987）](https://arxiv.org/pdf/2508.16987)
