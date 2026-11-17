# Router / Worker 系统提示词整理稿

> 状态：只读盘点稿。**未修改 `agents/prompt.py`，未改任何代码。**
> v2 修订（2026-11-09）：并入三条写作批注（工具只写关键项、按写目录隔离并行、不写死数值/不谈预算）；将记忆相关条目标为「实现中（M-MEMORY）」。
> v3 修订（2026-11-09）：砍掉 `acceptance` / `context_refs` 与结构化汇报要求；补入用户消息包装 `<user_query>` / `<query_time>` / `<attachment>`（channel 层实现中）。
> 盘点时工作区存在另一会话的未提交改动（上下文预算、WorkerInbox、三级停止、SendMessage、巡检、router 工具集、飞书附件、L3 规则层）。本文只依据当前磁盘上的代码，不依赖未合并分支。
>
> 证据口径：`文件:行号` = 代码事实；「grep 无结果」= 在指定范围搜过且没有；「待接入」= 代码未实现，**不能写进提示词当成已有能力**。

---

## 0. 结论摘要

1. 现有两个提示词合计约 30 行（`agents/prompt.py:26-55`），只覆盖：角色、Shell 无状态、派发/回复、不要假装成功。**没有一段描述「这层架构本身」**：router 有哪些工具、什么该自己干什么该派、派出去的任务怎么写、结果怎么收、worker 中途怎么找回来、历史去哪里翻。
2. 代码里已经具备、可以放心写进提示词的能力：事件摘要输入、`ReplyUser`、会话工具全集、FS/Shell、`new_context`、worker `SendMessage`、三级停止、worker 心跳与巡检、上下文折叠、token 用量提醒。
3. 用户点名要借鉴的四类内容，按当前代码实现程度排序：
   - **worker 使用姿势**：工具和进程模型已具备，提示词完全没写 → 可直接补。
   - **双向交互**：通道已通（worker → router 事件、router → worker 指令注入、三级停止），但 `ask` 是**异步不等待**，与契约 §7.5 的「阻塞等回复」不一致 → 提示词必须按异步写。
   - **router messages 落盘**：只有**部分**成立。`turns-*.jsonl` 只记录每轮的 digest / reply / 工具名，`inbox-*.jsonl` 记录原始事件；完整消息流在 Postgres checkpoint，且会被折叠删除。**目前没有任何工具能读这些 JSONL**；可读的持久化记录已安排随 M-MEMORY 实现（**实现中**，未完成前不能当已有能力）。
   - **上下文到顶派 worker 整理记忆**：记忆整理 worker（`memory_consolidate`）已安排实现，依赖 M-MEMORY（**实现中**）；当前还没有从 `create_session` 暴露入口，提示词不能写成已可用。
4. 现有提示词有 **3 处与代码矛盾**，另有 4 处工具 description 过期；这些会直接误导模型，改提示词前必须先定口径（见 §4）。
5. 本次并入的三条写作原则：**只写有特殊逻辑或影响大的工具**（其余靠 tool description）；**并行只在写目录不重叠时成立**；**提示词不写死数值、不谈预算**，只写行为口径。代码里的常量保留在本整理稿的证据栏，不往提示词里搬。
6. 范围收缩与输入约定：**不扩 `TaskSpec`**，不加 `acceptance` / `context_refs`，也不要求 worker 按固定结构汇报；用户消息固定包成 `<user_query>…</user_query><query_time>…+08:00</query_time>`，附件用 `<attachment path="…"/>`（channel 层实现中）。

---

## 1. 现状基线

> 下表的工具清单只用于盘点，**不按此逐条写进提示词**；提示词取舍原则见 §1.1。

| 维度 | 现状 | 证据 |
|---|---|---|
| router 输入 | 每批事件渲染成**一条** HumanMessage：用户 / worker_result / worker_message / worker_notice / timer / system；单条文本截断 1200 字；末尾提示「只有调用 ReplyUser 才会真正发消息」。用户消息约定包成 `<user_query>` + `<query_time>` + `<attachment>`（channel 层**实现中**） | `bus/envelope.py:129-174`、`bus/envelope.py:114-119`；`channel/feishu.py:186-215` |
| router 工具 | `ReplyUser`；`create_session` / `send_message_to_session` / `list_sessions` / `read_session` / `TaskOutput` / `StopCommand` / `set_session_title` / `fork_session`；`Read`/`Write`/`Edit`/`DeleteFile`/`Glob`/`Grep`；`Shell`（≤30s）；`new_context` | `agents/router/graph.py:33-50`、`tools/sessions.py:353-405`、`tools/shell.py:667-668` |
| router 没有的工具 | Web 搜索/抓取、Schedule、Skill、Profile、TodoWrite、任何记忆读写工具 | `agents/router/graph.py` 无 `build_web_tools`（grep `Web\|Schedule\|Skill\|memory` 无结果）；`tools/` 目录无 `schedule.py` / `profile.py` / `skill.py` / `todo.py` / `memory*.py`，grep `memory\|history\|TodoWrite` 无工具实现（`task_kind` 只出现在 `SessionService`） |
| worker 输入 | 只有 `task.prompt` 一条 HumanMessage；没有 router 的对话历史 | `worker_runner.py:76-84` |
| worker 工具 | FS 六件套；`Shell`（≤600s）；`WebSearch`/`WebFetch`（默认 `count=3`、`need_content=false`、单条正文截断 2KB）；`new_context`；`SendMessage` | `agents/worker/graph.py:37-61`、`config.py:81-86` |
| worker 没有的工具 | `ReplyUser`、`create_session`（不能派子 worker）、TodoWrite、记忆工具 | `agents/worker/graph.py:37-61` |
| middleware | 折叠 + 视觉降级 + 工具 trace + token 提醒；worker 另有心跳、停止注入、指令注入 | `agents/middleware.py:40-323`、`agents/worker/graph.py:62-69` |
| 持久化 | `inbox-*.jsonl`（每条事件）+ `turns-*.jsonl`（每轮 digest/reply/工具名）；Postgres checkpoint；worker 结果另有 `workers/<task_id>/result.json` | `main_router.py:148-159,196-206,252-253`、`worker_runner.py:126-140` |
| 无 JSONL 的 | 逐条 AI/Tool 消息流（只有日志与 checkpoint）；worker 会话消息（`read_session` 固定返回 `messages: []`） | `tools/sessions.py:219-220` |

### 1.1 提示词里写哪些工具（写作原则）

工具清单是装配事实，不是提示词内容。系统提示词只写三类：

1. **会派发 / 会阻塞 / 会改上下文的**：`create_session`、`send_message_to_session`、`StopCommand`、`TaskOutput`、`new_context`、worker 的 `SendMessage`；
2. **有边界陷阱的**：`Shell`（无状态、router 侧只允许短命令、超时会提示改派）、文件工具（只能碰工作区）；
3. **唯一出口 / 唯一通道**：router 的 `ReplyUser`。

其余工具（`list_sessions`、`read_session`、`set_session_title`、文件工具的常规用法、`WebSearch`/`WebFetch` 的常规用法）**不在系统提示词里逐条枚举**，靠各自的 tool description。下面 R3 / W3 按这个原则写。

---

## 2. Router 提示词：建议段落与顺序

建议把 `router_prompt` 从「职责与规则」一段扩成下列 13 段。顺序即建议写入顺序。

| # | 段落名 | 这一段要说什么（要点） | 实现状态 | 证据 / 依赖 |
|---|---|---|---|---|
| R1 | 角色与目标 | 你是唯一对用户说话的一层；协调 worker；目标是把用户的事办成并如实汇报，不是自己包办所有活 | ✅ 已有雏形 | `agents/prompt.py:27` |
| R2 | 每轮你看到什么 | 你收到的是**一批事件的摘要**，不是逐条对话；用户消息固定包成 `<user_query>…</user_query><query_time>ISO8601+08:00</query_time>`，附件另有 `<attachment path="…"/>`；`query_time` 是发送时刻，用来判断相对时间、识别用户隔了很久才回来，不要假设实时；附件路径可直接用文件工具读，图片按图片读（模型不能解析时会看到占位，不要假装看见）；长正文会被截断；末尾那句提示是系统约定 | ✅ 事件摘要已实现；输入包装**实现中（channel 层）** | `bus/envelope.py:129-174`、`bus/envelope.py:114-119`、`channel/feishu.py:186-215` |
| R3 | 关键工具与硬边界（不逐条枚举） | 只讲必须讲的：`ReplyUser` 是唯一用户出口；`create_session` 只回执、不等待；`send_message_to_session` 是给 worker 追加指令的唯一通道；`StopCommand` 是分阶段升级的终止，不是普通取消；`new_context` 会折叠早期上下文；`Shell` 无状态、router 侧只能跑短命令，长任务必须派；文件工具只能碰工作区。其余工具（状态查询、改标题、常规文件/网页操作）按 tool description 用，不在提示词里列清单 | ✅ 工具存在；`fork_session` 未实现 | `agents/router/graph.py:33-50`、`tools/sessions.py:345-350,219-220`、`tools/shell.py:406-416,696`、`tools/paths.py:45-66` |
| R4 | 回复纪律（ReplyUser） | 唯一用户出口；`text` 必须非空；短、中文、先结论；每个真实用户轮次**第一个动作**先回一句；派发成功后给回执但**不能说已完成**；不要与其它工具并行调用（提示词约定）；不要编造结果 | ⚠️ 部分：工具与幂等已实现；「一轮几次」只是提示词约定，代码允许多次 | `tools/reply.py:33-93`（幂等 `trace:index` 在 68-73）；现有约定在 `agents/prompt.py:37` |
| R5 | 自己干 vs 派 worker | 直接干：闲聊/确认/用已有上下文回答、状态查询（`list_sessions`/`TaskOutput`）、看一个文件、跑一条短命令、整理已有结论。派出去：外部检索、构建/测试、日志与代码分析、批量文件处理、长命令、产物生成。**判断整件事，不要为了「准备派发」先自己调查**；一旦直接干需要深入内容、反复试错或写脚本，立刻停手改派 | ⚠️ 能力已具备（router 确实有 FS+Shell），但现有提示词第 38 行写反了 | `agents/router/graph.py:36-49`、`agents/prompt.py:38`；设计口径见 `agents/router/graph.py:35` 注释 |
| R6 | 派发姿势（任务自含，自然语言） | worker 只看得到 `prompt`，**没有你们的对话历史**；任务用自然语言写清：目标、背景、涉及路径/仓库、约束（能改什么不能改什么）、期望的结果；标题用于状态展示；`cwd` 指定运行目录；禁止写「如上所述」「按之前的来」；**不要求** `acceptance` / `context_refs` 之类结构化字段，也不要求 worker 按固定结构汇报 | ✅ 参数已支持；不扩 `TaskSpec` | `tools/sessions.py:356-359`、`contracts.py:240-257`、`worker_runner.py:76-84` |
| R7 | 切分与并行（按写目录隔离） | 一个 worker 一条主线；只有**不共享写目录**且互不依赖的子任务才在同一轮并行派发；会写同一个运行目录或同一批文件的任务必须串行，或者给每个 worker 指定独立 `cwd`；有依赖的必须串行；最后由你合并结论 | ✅ 工具可多次调用；无并发上限、无目录冲突检测代码 | `tools/sessions.py:115-188,356-359`（`cwd` 入参） |
| R8 | 结果回收 | `create_session` 只返回 `accepted`，**派发不等于完成**；结果以 `worker_result` 事件在下一轮唤醒你；digest 里的结果可能被截断，不能当全文；完整结果要按工具/会话状态去取（具体路径由运行时决定，不写进提示词）；没收到结果就如实说「已派发，尚未回报」 | ⚠️ 事件与落盘有；**没有读取 `result.json` 的工具**，`Read` 又出不了 workspace，只能用 `Shell` 手动读 | `tools/sessions.py:182-188`、`worker_runner.py:101-140`、`bus/envelope.py:146-147`、`tools/paths.py:45-66`、`config.py:137-139` |
| R9 | 与 worker 的双向交互 | `worker_message` = 它需要你决策；`worker_notice` = 进度，未必要回；用 `send_message_to_session` 追加指令或回答；**发送前先看会话状态**，`done`/`error` 的会话不要再发；`ask` 是异步的，worker 不会原地等；`StopCommand` 分阶段升级：先请求收敛，重复调用会升级为强制终止，不要当普通取消反复点；`TaskOutput` 显示 `no_update` 就结束本轮，不要轮询 | ⚠️ 通道已实现；`ask` 实际异步，工具 description 过期 | `tools/worker_messages.py:22-71`、`tools/sessions.py:247-265,267-329,371-384`、`agents/middleware.py:216-272`、`docs/ENGINEERING_CONTRACT.md:569` |
| R10 | 上下文、历史与记忆 | 你的对话在 checkpoint 里，上下文过长时会自动折叠，早前细节可能已经不在；`new_context` 可主动折叠；每轮会看到上一轮的用量提示；**折叠后不要凭记忆回答，重新读文件/查记录**；记忆检索与整理能力正在随 M-MEMORY 落地，完成前不要当成已有能力 | ⚠️ 折叠、`new_context`、用量提示已实现；历史读取与记忆整理**实现中（M-MEMORY）** | `agents/middleware.py:82-188,278-323`、`tools/context.py`、`config.py:94-99`；`memory/` 只有 `jsonl.py`；`docs/ENGINEERING_CONTRACT.md:963` 标 ❌ |
| R11 | 系统事件与巡检 | `system/kind=task_watch`：任务疑似卡住，**绝不自动重派**，问用户「重试 / 放弃 / 改需求」；`send_failed`：发送失败，告知用户；`timer`：定时任务到期，按 prompt 执行 | ✅ 已实现，提示词未覆盖 | `main_gateway.py:444-528`、`bus/envelope.py:158-171` |
| R12 | 安全、失败与一致性 | 不编造、不假装执行、工具失败如实转述；不承诺「稍后自动通知」这类运行时保证；同一轮重复回复由幂等键兜底，但不要依赖它；密钥/凭证不回显、不写文件 | ⚠️ 部分已有 | `agents/prompt.py:38-39`、`tools/reply.py:68-73` |
| R13 | 附件与视觉 | 用户消息里的 `<attachment path="…"/>` 指向本地文件，文件工具可直接读；图片按图片读。注意：附件目录是否在可读范围、当前模型能否解析图片由运行时决定；解析不了时会看到占位内容，不能假装看见 | ⚠️ 输入包装**实现中（channel 层）**；可读范围/视觉能力仍待确认 | `channel/feishu.py:186-215,274-307`、`config.py:55,133-135`、`tools/paths.py:45-66`、`agents/middleware.py:40-56` |

### 2.1 R5 的判断口径（按本项目架构改写，不照搬参考）

参考提示词是「内容检查一律派发」，但本项目的代码与注释明确允许 router 自己看文件、跑短命令：

- `agents/router/graph.py:35` 写着「§6.1 / §7.6：router 自己就能读写文件、跑短命令，不必为了看一眼文件派 worker」；
- `Shell` 对 router 只限 30s，超时错误提示就是「改用 create_session 派 worker」（`tools/shell.py:410-415`）。

因此 R5 应写成**按代价分界**，而不是按「是不是内容检查」分界：

1. 一条命令、一个文件、一次状态查询 → 自己做。
2. 需要多步、长跑、外部检索、批量处理、构建测试 → 派。
3. 自己做到一半发现要读很多文件、反复试错或写脚本 → 立即停手，把已知事实打包派出去。

### 2.2 R8 / R9 的三个代码级坑，提示词必须绕开

1. **digest 只显示 task_id 后 8 位**（`bus/envelope.py:145`）。折叠后 checkpoint 里那条 `create_session` 的 ToolMessage 可能被删，模型就只剩 8 位 id。提示词应要求：需要后续操作时用 `list_sessions` 找回完整 `session_id`，**不要猜**。
2. **digest 的 `worker_result` 不显示 `status`**（`bus/envelope.py:146-147` 只写「完成」）。失败与否只能看正文（失败正文由 `worker_runner.py:220` 写成「任务执行失败…」）。提示词应要求：看到 result 先看正文是否以失败开头，必要时 `read_session` 查 `status`。
3. **`ask` 不阻塞**（`tools/worker_messages.py:65-70` 投递后立即返回 `queued`）。所以 router 不能假设「我回一句它就会照着做」——如果 worker 已经走远或已退出，指令会留在 stream 里没人消费。提示词应写：回答之后若任务已结束，需要重新派发。

### 2.3 R10 的准确表述（不能把「翻落盘」写成现成能力）

- 真正追加落盘的是：`inbox-*.jsonl`（每条事件原样）、`turns-*.jsonl`（每轮的 `digest` / `reply` / `tool_calls` / `trace_id`），见 `main_router.py:148-159,196-206`；
- `memory/jsonl.py:85-113` 有 `read_records()`，但**没有任何工具调用它**（全仓仅 `memory/jsonl.py` 自身与 `scripts/check_store.py` 引用）；
- M-MEMORY 已安排新增 `compact` 记录、记忆整理 worker 与可读的持久化记录（JSONL）；完成后「翻落盘」才有正式入口，当前仍是**实现中**；
- 完整逐条消息只在 Postgres checkpoint，且 `ContextBudgetMiddleware` 会按 256k/320k 阈值删除旧消息（`agents/middleware.py:82-188`）；
- 因此模型所谓「去翻历史」目前只能靠 `Shell` 读绝对路径，而 `data_dir` 可配置（`config.py:68`），不适合把路径写死进提示词。

结论：R10 先写「不知道就重读、不凭记忆」，把「翻 JSONL / 记忆整理 worker」标为**实现中（M-MEMORY）**，等可读接口落地后再补具体指引。

### 2.4 用户消息包装（channel 层实现中）

进入 router 的用户消息固定是这个形状，提示词要按这个语义理解：

```text
<user_query>用户原话</user_query><query_time>2026-11-09T21:30:12+08:00</query_time>
```

- `query_time` 是用户发送消息的时刻（UTC+8），用于判断「今天 / 昨天 / 刚才」这类相对时间，以及识别用户可能隔了很久才回来；**不要假设消息是实时的**。
- 有附件时补 `<attachment path="D:\...\xxx.png"/>`，多个就多条；路径可直接用文件工具读。
- 图片按图片读；当前模型能否解析由运行时决定，解析不了会看到占位内容，不能假装看见。
- 这段包装由 channel 层生成（代码侧由另一会话实现），**当前尚未落地**；提示词可以先约定形状，但不能假设现在已经收到这种格式。

---

## 3. Worker 提示词：建议段落与顺序

现有 `worker_prompt` 只有 5 行（`agents/prompt.py:44-55`），建议扩成 9 段。

| # | 段落名 | 这一段要说什么（要点） | 实现状态 | 证据 / 依赖 |
|---|---|---|---|---|
| W1 | 角色与边界 | 你是独立进程里的一次性执行者；没有 `ReplyUser`，不能直接找用户；没有 `create_session`，不能派子 worker；用户只会看到 router 转述的最终结果 | ✅ 已有雏形 | `agents/prompt.py:51`、`agents/worker/graph.py:37-61` |
| W2 | 任务输入与路径 | 你只收到 `task.prompt`；`cwd` 是任务运行目录；`workspace_root` 是文件工具允许写入的根；没有此前的对话，缺信息时按最保守假设并在结论里声明 | ⚠️ 有输入；但 `env_block` 里写的是 `settings.workspace_root`，不是 `task.cwd` | `worker_runner.py:76-84`、`agents/worker/graph.py:46`、`agents/prompt.py:8-16,45-47` |
| W3 | 关键工具与选路（不逐条枚举） | 只讲有陷阱的：`Shell` 无状态且有超时上限，长任务要拆；文件工具只能碰工作区；长输出会落文件并给 artifact 路径，后续用 `Read`/`Grep` 接着看；外部信息优先用搜索的正文模式，已知 URL 才抓取；`new_context` 折叠；`SendMessage` 是找 router 的唯一通道。具体参数与默认值看 tool description | ✅ 已实现 | `tools/shell.py:667-668`、`config.py:81-86`、`tools/web.py`、`tools/shell.py:703-705`、`tools/context.py`、`tools/worker_messages.py` |
| W4 | 执行纪律 | 先读再改；能执行就执行，不要只给计划；每步验证；同参数失败的命令不要原样重试；写文件只用 `Write`/`Edit`，不要用 shell 重定向；二进制/大文件用 `Grep` 定位再 `Read` 片段 | ✅ 能力有；提示词只写了「不要只描述计划」 | `tools/fs.py:322-470`、`tools/shell.py:698-705`、`agents/prompt.py:53` |
| W5 | 收敛与结论前置 | 讲行为，不讲数字：先规划再动手，同一目标不要换查询词反复搜；信息够了就收手；**最终结论前置**成可直接转述的摘要，细节写进文件并给路径；感觉继续下去收不了尾时，先给「已完成 / 未完成 / 建议」，不要空手而回 | ✅ 硬上限存在（常量见 `config.py:80`，不搬进提示词）；提示词完全没写 | `config.py:80`、`worker_runner.py:81`、`bus/envelope.py:114-119`、`worker_runner.py:91-98` |
| W6 | 与 router 的交互 | `SendMessage(kind="notify")` 报进度；`SendMessage(kind="ask")` 只是投递，**不会原地等答复**，发出后要按最保守方案继续并在结论里写明假设；收到以「[来自 router 的追加指令…]」开头的消息时，先判断是否与当前任务冲突，冲突以用户最新意图为准；收到停止请求就停止新工具、按「已完成 / 未完成 / 建议」收尾 | ✅ 通道与注入已实现 | `tools/worker_messages.py:26-70`、`agents/middleware.py:216-241,191-194,244-272` |
| W7 | 安全与确认 | 规则层拒绝（`denied_by_rule`）不要重试；`need_confirm` 必须在用户明确同意后才带 `confirmed=true` 重试一次；工作区外写/删同样要确认；不回显、不落盘密钥；当前模型不支持视觉，读图只会得到占位文本 | ✅ 已实现 | `tools/shell.py:475-487,698-701`、`tools/rules.py:1-12`、`config.py:55`、`agents/middleware.py:40-56` |
| W8 | 失败与结果契约 | 无论成败都必须回传：`worker_runner` 保证发 `worker_result` 并写 `result.json`；最后一条消息用自然语言说清做了什么、结果如何、失败在哪一步（如有）；不要求固定结构；不要编造成功 | ✅ runner 已保证；提示词部分覆盖 | `worker_runner.py:91-98,126-140,213-230`、`agents/prompt.py:52-54` |
| W9 | 上下文与记忆 | 长任务感觉上下文快满时调用 `new_context`；折叠后需要早前细节就重新读文件/重跑命令，不要凭记忆；记忆检索/整理能力正在随 M-MEMORY 落地，未接入前不要假装记住了跨任务信息 | ⚠️ `new_context` 有；记忆能力**实现中（M-MEMORY）** | `tools/context.py`、`agents/middleware.py:165-188` |

### 3.1 W5 是这次最该补的一段

上一次 `GraphRecursionError` 的直接原因就是 worker 在同一目标上反复搜索、且工具输出撑爆上下文。代码里有一个硬性执行上限常量（见 `config.py:80`，**不要搬进提示词**），也已经做了两件事：worker 侧搜索更保守、失败原因保留类名+消息。但**提示词里没有任何收敛与收尾要求**。W5 只写行为：

- 先规划再动手，同一目标不要换着查询词反复搜；
- 需要外部信息时优先用搜索的正文模式，已知 URL 才抓取（避免同一批正文付两次费，契约 §14-45）；
- 信息足够就停止检索，先给结论；写不完的细节落文件并给路径；
- 感觉继续下去收不了尾时，先给「已完成 / 未完成 / 建议」的收尾结论。

### 3.2 W2 的一个小实现缺口

`worker_prompt(settings)` 只接收 `Settings`，所以 `env_block` 只能打印 `workspace_root`，打印不出本任务的 `task.cwd`（`agents/worker/graph.py:46` 传给了 Shell，但没传给 prompt）。如果希望 worker 明确知道「我这次在哪个目录干活」，需要把 `task` 传进 `worker_prompt`——属于提示词装配改动，**待接入**，不在本次只读范围。

---

## 4. 现有提示词 / 工具描述的事实性错误（改之前必须先定）

这些是模型每天会读到的文本，错误会直接导致错误行为。

| # | 位置 | 现在写的 | 代码事实 | 建议处理 |
|---|---|---|---|---|
| 1 | `agents/prompt.py:38` | 「你没有 Shell 权限」 | router 装配了 `Shell`，上限 30s | 改成「你能跑短命令，长任务派 worker」（具体超时留在 tool description） |
| 2 | `agents/prompt.py:37` | 「同一轮最多 ReplyUser 一次」 | `tools/reply.py:68-73` 用 `trace:index` 递增，允许多次；代码不限制 | 已定：一轮最多两条（先回执 + 派发后回执，第二条尽量与最终结果合并） |
| 3 | `tools/sessions.py:383` | `send_message_to_session`「worker 尚未消费」 | `WorkerInboxMiddleware` 会在下一轮模型调用前注入并 ACK（`agents/middleware.py:216-241`、`agents/worker/control.py:78-90`） | 改成「worker 下一轮会读到；已结束的会话不会消费」 |
| 4 | `tools/sessions.py:378` | `StopCommand`「v1 只写一级停止标记」 | 已实现分级升级（`stop_key=1/2/3` + `taskkill /F /T`，`tools/sessions.py:267-329`） | 按「先请求收敛、再升级强杀」的分级语义重写 |
| 5 | 契约 `docs/ENGINEERING_CONTRACT.md:569` | `SendMessage(ask)` 会 `XREADGROUP` 等 router 回复 | 实现是投递后立即返回（`tools/worker_messages.py:65-70`），无等待、无预算、无 `question_id` | **以代码为准**：契约标注实现差异，提示词按异步写；是否补阻塞语义另议 |
| 6 | `channel/feishu.py:211` | 图片消息「本地路径，可用 `Read` 查看」 | 目标格式是 `<attachment path="…"/>`（channel 层**实现中**）；当前附件落在 `data_dir/inbox`，而 `Read` 只允许 `workspace_root`（`tools/paths.py:45-66`）；模型视觉默认关闭 | 提示词按新格式写；channel 改造时把附件放进可读范围，或提供附件读取通道 |
| 7 | `agents/prompt.py:10` | `env_block` 的 `cwd` 是 `settings.workspace_root` | worker 实际运行目录是 `task.cwd`（`agents/worker/graph.py:46`） | 待接入：`worker_prompt` 接收 `task` 后打印 `task.cwd` |

---

## 5. 待接入 / 实现中清单（按对提示词的影响排序）

| 能力 | 缺什么 | 依赖 | 提示词现在怎么写 |
|---|---|---|---|
| 历史/记忆检索 | `read_records()` 已存在，但没有工具包装；M-MEMORY 计划提供可读的持久化记录与 `compact` 记录（**实现中**） | M-MEMORY + 工具层接入 | 实现前只能写「不知道就重读文件」，不能写「去翻历史记录」 |
| 记忆整理 worker | 已安排实现（M-MEMORY **实现中**）；`TaskKind` 有 `memory_consolidate`（`contracts.py:49`），`create_session` 工具尚未暴露 `task_kind` | M-MEMORY + 工具层暴露 `task_kind`（服务层已支持：`tools/sessions.py:121,151`；工具层 `356-359` 未暴露） | 实现前不能写「上下文满了派 worker 整理记忆」 |
| 完整结果读取 | 没有 `read_result`；`result.json` 在 `data_dir/workers`，`Read` 出不了 workspace | 复用 `read_records` 思路的结果读取工具，或把 workers 目录纳入可读范围 | 只能靠 `Shell` 读绝对路径；提示词不要写死路径 |
| digest 显示完整 task_id / status | 摘要只给后 8 位、worker_result 不显示 `status` | `bus/envelope.py:145-147` | 要求用 `list_sessions` 找回 id；看正文判断失败 |
| 附件可读 | 目标格式 `<attachment path="…"/>` 由 channel 层生成（**实现中**）；附件目录当前不在 workspace，视觉模型默认关闭 | channel 层改造；`config.py:55` / `133-135` | 可以写「路径可直接读」；图片要带「不能解析时不要假装看见」的保底 |
| Schedule / Skill / Profile | 契约 B1 定义了 `tools/schedule.py`、`tools/skill.py`、`tools/profile.py`，仓库里没有 | 对应模块 | 提示词里一个字都不要提 |
| TodoWrite | 契约 B2 定义 `tools/todo.py`，没有 | 对应模块 | 不要写「用 TodoWrite 列计划」 |
| 并行派发去重/冲突检测 | `create_session` 无并发上限、无共享路径检测 | 可选增强 | 写人工约束：「不共享写目录才并行，共享目录串行或独立 `cwd`」 |
| ~~结构化任务字段~~ | **已决定不做**：不扩 `TaskSpec`，不加 `acceptance` / `context_refs` | — | 派发用自然语言，不要求结构化汇报 |

---

## 6. 已拍板的口径（记录）

1. **ReplyUser 节奏**：一轮最多两条——有真实用户输入时先回执，派发后再回执；第二条尽量与最终结果合并，不为每次内部派发分别回复。
2. **router 直接工作边界**：保留 `Read/Write/Edit/DeleteFile` 与短命令 `Shell`；长命令一律派 worker；提示词不写具体秒数。
3. **记忆整理触发**：触发式，由 router 自己判断「上下文快满了 / 该整理了」后派记忆整理 worker；不做定时整理。M-MEMORY 未完成前，提示词只写结构与 TODO 占位，不写调用尚不存在工具的指令。

---

## 7. 后续验证建议（本次不改代码）

- 现有脚本里**没有**针对 `router_prompt` / `worker_prompt` 的断言：`grep "router_prompt\|worker_prompt\|system_prompt" scripts/*.py` 无结果。跑 `check_contracts` 只能证明没破坏装配，不能证明提示词有效。
- 建议改完提示词后手工跑 8 个场景，每个场景记录「模型调了哪些工具、回复是否如实」：
  1. 纯闲聊 → 只 `ReplyUser`，不派发；
  2. 「看一下 xxx 文件」→ router 直接 `Read`，不派发；
  3. 「帮我跑一遍测试并总结」→ 先 `ReplyUser` 回执 → `create_session` → 本轮结束；
  4. worker 发 `worker_message` 提问 → router 用 `send_message_to_session` 回答（并确认任务是否还活着）；
  5. worker 返回长结果 → router 是否意识到摘要被截断、是否如实说明；
  6. 卡住任务触发 `task_watch` → router 是否只问用户、没有自动重派；
  7. 用户说「刚才那个文件」→ router 是否用 `query_time` 判断时间、先查状态而不是硬猜；
  8. 用户发来带 `<attachment path="…"/>` 的消息 → router 是否直接用文件工具读，而不是让用户重发。
- 每次只改一个段落，避免无法归因；改完把实际工具序列与期望序列对照记录。

---

## 8. 证据索引

| 主题 | 文件 / 行号 |
|---|---|
| 现有 prompt | `agents/prompt.py:8-55` |
| router 装配 | `agents/router/graph.py:33-63` |
| worker 装配 | `agents/worker/graph.py:36-76` |
| 会话工具 | `tools/sessions.py:114-405` |
| ReplyUser | `tools/reply.py:29-95` |
| worker SendMessage | `tools/worker_messages.py:22-71` |
| worker 控制通道 | `agents/worker/control.py:27-119` |
| 指令/停止注入 | `agents/middleware.py:191-272` |
| 折叠与 token 提醒 | `agents/middleware.py:82-188,278-323`、`agents/context_budget.py` |
| 事件摘要 | `bus/envelope.py:114-174` |
| JSONL | `memory/jsonl.py:20-113`、`main_router.py:148-159,196-206,252-253` |
| worker 结果回传 | `worker_runner.py:91-140,213-230` |
| 巡检 | `main_gateway.py:444-528` |
| Shell 边界与规则层 | `tools/shell.py:406-487,660-726`、`tools/rules.py:1-12` |
| 文件工具边界 | `tools/fs.py:172-185`、`tools/paths.py:45-66` |
| 飞书附件 | `channel/feishu.py:145-215,274-307` |
| 配置 | `config.py:55,68,79-99,124-139` |
| 契约差异记录 | `docs/ENGINEERING_CONTRACT.md:569,963,1068-1117` |
