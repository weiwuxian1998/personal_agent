# Router 记忆与上下文管理设计（讨论稿）

> 状态：**讨论稿 v0.1**（2026-11-19），未实施、未改任何代码。
> 本文记录用户已定方向、助手分析建议与待讨论问题，作为下一轮讨论与后续契约变更（§11 流程）的底稿。
> 标注约定：【已定】= 用户明确的方向；【建议】= 助手观点，待确认；【待讨论】= 开放问题。

---

## 1. 背景与定位

- 场景是**单会话 personal agent**：router 只有一个长驻线程（`router:main`），不存在"跨会话带记忆"的问题；
  记忆进入模型的唯一通道是**注入层**，上下文管理是持续性的，不是一次性的。
- 现有 `memory/`（jsonl / ledger）、`tools/memory.py`、折叠时的记忆快照注入等代码是**早期 coding 的占位实现**，
  不是目标形态【已定】。本文不以其为基础做增量修补，而是重新定义目标，再盘点哪些机件可以复用。
- 目标节奏（sleep-time compute / dreaming）：
  - **白天**：router 轻量分派任务，上下文一般不长（百万窗口模型下白天够用）；router 可主动管理上下文
    （`new_context` 是提供给 router 的工具【已定】），并可随手写下"希望夜间整理时关注的侧重点"。
  - **夜里**：派专门的记忆 worker，综合「当天上下文流水 + router 侧重点 + 历史记忆」做整理，
    更新记忆并更新 router 上下文。
- **worker 不需要自己管理上下文**，现有通用 worker 机制对普通任务够用【已定】；
  但记忆 worker 的读取模式特殊（大量文本），通用机制需要为它做差异化调整【已定】。

## 2. 参考框架与结论

### 2.1 Anthropic《Components of agent memory》

四层：**transcript（原始流水）→ notes（笔记）→ representation（wiki 等组织化表示）→ injected context（注入层）**。

dreaming 流水线四步：
1. **Note creation**：活跃会话期间随手标记显著事件 / 写观察（对应我们的"router 白天侧重点"）；
2. **Validation**：离线时拿笔记对照原始 transcript 校验，纠正错误（文中 Pokémon 例子：错误地点笔记被
   dreaming 纠正后进度显著推进）；
3. **Representation building**：把笔记与日志综合成**互相链接的 wiki 页面**，多遍增量构建，
   期间探索、写页、纠错、合并重复；
4. **Promotion**：把值得的内容上浮到注入层。

关键事实与教训：
- 注入层就是 `MEMORY.md` 的**前 200 行 / 25KB 索引**，不是全量记忆；"Claude 不加载全部 auto memory"。
- 有效的 Pokémon 记忆约 **10 个文件、4.3 万字符**；低效做法生成几百个文件。
- **wiki 比原始语料还大 = 抽象失败**；每遍 dreaming 必须包含合并 / 归档动作。
- salience（什么值得记）**交给模型自由判断**，死规则反而降低效果。
- 记"屏幕上看不到的策略与规则"（非冗余信息），不要无差别记录。

### 2.2 阿里 AgentScope ReMe（Remember Me, Refine Me）

- 理念：**文件即记忆**——本地 Markdown 工作区（`.reme/`），分**日记**（当日内容）与**长期知识**
  （稳定事实、偏好、流程）；按段落 + 笔记链接检索；索引可从文件重建；多 agent 共享工作区；带 Studio 可视化。
- 评估【建议】：它的"日记 → 长期知识"两级结构与我们的"按天 transcript → wiki"几乎同构，理念值得借鉴；
  但它是面向多 agent 的通用基础设施（服务化部署、BM25/向量索引、Studio），对**个人画像级的小记忆量**过重。
  **借结构，不引依赖**：手写一个 md 文件子集即可，检索用索引页 + 关键词回查，不上向量/图谱。

### 2.3 对本场景的结论

个人 agent 的记忆主要是**用户画像、偏好、约定、进行中事项、少量项目/环境事实**，总量小（预期 ≤10 个页面、
几十 KB 量级），整理复杂度远低于"从文档抽取知识"的场景。因此：
- representation 层 = **一小撮主题 md 页 + 一个索引页**，不需要数据库/搜索引擎；
- 注入层 = 索引页（≤200 行），其余页按需读；
- dreaming 的校验成本也低：每条结论带出处，回查当天 transcript 即可。

## 3. 目标架构总览

```
白天                                    夜里（如 03:30）
─────                                   ─────────────────
router（router:main）                   ScheduleScanner 按类派发【已定】
  │  每条消息产生时                        │  worker 类 → 直接拉起（夜间整理属此类）
  ├─> transcript-<日期>.jsonl（按天流水）    │  router 类 → 照旧投 timer（提醒用户/提醒整理记忆）
  │  fs Write/Edit 写每日笔记            ▼ 记忆 worker（task_kind=memory_consolidate）
  ├─> notes/<日期>.md（侧重点，router/notes/） 输入三合一：
  │                                      ① 当天 transcript（分页流式读）
  │  上下文超预算 → 被动折叠（安全网）      ② 当天 notes（router 侧重点）
  │  主动 new_context → 折叠+注入索引      ③ 现有 wiki / index（历史记忆）
  │                                      三遍 dreaming：提取 → 校验整合 → 重建注入层
  ▼                                      产出：wiki 页更新 + index.md + 当日 threads 快照 + 报告
早晨 router 被唤醒时：                     │
  上下文 = 最近窗口 + 最新注入层            ▼
  （index + threads + 热点事实）          worker_result → router（仅完成/有疑问时才唤醒【已定】）
                                         router 记账 + 标记上下文待更新（懒折叠【建议】）
```

## 4. Transcript：router messages 按天记录【已定方向 + 关键技术约束】

用户方向：transcript 就是 router 的 messages（累计的大模型上下文），按天记录；夜间 worker 读当天记录整理。

**关键约束【建议，必须正视】**：不能"夜里从 checkpoint 把当天 messages dump 出来"。
现有折叠是逐条 `RemoveMessage`——白天一旦触发被动折叠，checkpoint 里**已经没有被删掉的历史**。
所以按天流水必须在**消息产生时**追加记录（journal-at-production），而不是事后从 checkpoint 导出：

- 记录点：中间件层（`before_model` 入口记 HumanMessage / 注入块，`after_model` 记 AIMessage，
  工具结果记 ToolMessage），或等价的统一钩子；只追加、不改写。
- 文件：`DATA_DIR/router/transcript/transcript-YYYY-MM-DD.jsonl`，日界按 `TZ_SHANGHAI`（契约 §9.3）。
- 每行一条消息：`{ts, day_seq, msg_id, role, content, tool_calls?, tool_call_id?}`；
  复用 `JsonlWriter` 的 append + flush + fsync 模式（崩溃最坏丢最后一行）。
- 体量评估：router 只分派不干活，单日流水量可控；worker 侧分页读（见 §7）。
- 与占位代码的关系【已核实 2026-11-21】：`turns-*.jsonl` **正在被控制台使用**——
  adapter `GET /api/turns` 从中读 digest 记录（protocol_adapter.py），前端 RouterContext 页
  「每轮记账（turns.jsonl）」子页签展示；`inbox-*.jsonl` 同理（`/api/inbox`）。
- **turns/inbox 移出 memory 区【已定：用户 2026-11-21】**：它们是记账，不是记忆，"不能掺和在一起"。
  目标 `DATA_DIR/router/ledger/`（目录名待定）；写方（main_router 记账）与读方
  （adapter `/api/turns`、`/api/inbox`）同步改目录，API 形状与 UI 不变，存量文件整体迁移。
  transcript 另立做全量记忆素材（`router/transcript/`，同样在 memory 区外）；
  `facts.jsonl / decisions.jsonl / summary.md` 占位记忆待 wiki 形态定稿后决定迁移或废弃【待讨论】。

## 5. Router 白天侧重点：每日笔记文件【已定方向：用户 2026-11-21】

**不新增工具**。router 用现有 fs 工具（Write/Edit）直接写每天的固定文件，相当于助理的每日日志：
"用户这个偏好要记住"、"今晚重点整理打包项目的进展"、"这个结论是临时的，夜里核实"。

- 路径约定【建议调整 2026-11-21】：`DATA_DIR/router/notes/YYYY-MM-DD.md`——**notes 也移出 memory 区**，
  与 turns 同一逻辑：素材不是记忆。
  一天一份，与 transcript 同日界（Asia/Shanghai）；router 白天随手追加，夜间 worker 用通用 Read 读取。
- 写权限边界【已定：用户 2026-11-21；2026-11-21 二次修订：**不做硬限制**】：
  **约定上 router 只写 `notes/*.md`**（用户自己也可以写）；memory 区其他文件 router 不写——
  transcript 由系统自动保存，wiki/index/threads 由专属 worker 整理写入（完整矩阵见 §6.2）。
  **这条边界只写进系统提示词做约定，不在 fs/Shell 工具里做隔离或专属 write_roots**——
  与 worker 侧同一哲学：合法路径内不设额外沙箱，靠纪律不靠强制。
  （**已落地 2026-11-21**：用户在 `.env` 配 `PLAY_ROOT=D:\`，fs 读写删与 Shell R8 确认线覆盖整个
  D 盘，`DATA_DIR/router/notes/` 直接可写；"notes 暂放 play_root"的过渡方案作废。
  盘符根 `D:\` 本身仍在禁区（rules.py 只禁根，不影响任何子路径）；C 盘只读区/禁区规则不变。）
- 路径 + 日期约定写进 router_prompt；文件就是普通 markdown，不强制 schema。
- 附带好处：这份文件是本地 markdown，**用户可见可编辑**——用户也可以直接在今日笔记里留一句话
  给夜间 worker，成为人机共用的"留言板"。
- 定位【建议】：notes 是**注意力引导，不是指令清单**。worker 仍自主判断 salience（Anthropic 结论：
  死规则降低效果）；notes 里"必须记住"级别的条目应优先上浮注入层。
- 这与 Anthropic 流水线第 1 步（活跃期间 note creation）完全对应，成本近乎零（一条 prompt 约定 + 每天一个文件）。

## 6. Wiki 到底是什么：个人 agent 的具体形态【已定方向：用户 2026-11-21 + 建议】

本质：**agent 自己维护的一小撮 Markdown 主题页 + 页间链接 + 一个索引页**。不是数据库，不是向量库。
Anthropic 文中 wiki 就是"Claude 写并维护的页面集合"，靠链接成地图，检索 = 搜索工具 + 顺链接（两者结合最好）。
个人规模下"搜索工具"可以退化为：索引页地图 + `ReadMemoryPage(name)` + 关键词回查 transcript。

文件组织【已定方向：用户 2026-11-21】：**只规定两类结构性必备文件，其余自由生长**——
Anthropic 的结论是不要给 agent 过多限制，提前钉死页面集（people/decisions/profile…）属于过度设计；
页面结构应由整理 worker 根据运行情况自行决定。设计只钉死承载**系统功能**的两类：

```
DATA_DIR/router/
  notes/            # [素材] router/用户每日笔记（见 §5）
  transcript/       # [素材] 系统自动按天记录的全量消息流水（见 §4）
  ledger/           # [记账] turns/inbox 摘要流水，供控制台（见 §4；目录名待定）
  memory/           # [记忆] 这里只放记忆本身
    index.md        # [必备] 注入层（唯一常驻上下文的页）：≤200 行 / ≤8KB
                    #   用户是谁、当前关注什么、页面地图（每页一行摘要+链接）、关键事实速览
    threads/        # [必备] 进行中事项，按天快照：threads-YYYY-MM-DD.md（见 §6.1）
                    #   每夜 dreaming 承接昨日 + 应用当日变更；最新一份 = 当前状态
    <其余页面…>      # [自由] 整理 worker 按需创建/合并/归档，不预设页名
                    #   举例（非规定）：用户画像、某个项目、环境事实、决策日志、人物……
    journal/        # [可选] 每夜整理日志：当晚做了什么、covers 区间、版本变化
    scratch/        # [工作] 整理 worker 的中间草稿区，完成后清理
```

两个必备项之所以必备：index 是**注入层契约**（折叠 / new_context 靠它知道注入什么），
threads 是**连续性保障**（open 事项必须有固定存放处，不会在自由整理中丢失）。
除此之外不设 schema——下面的页面纪律约束的是"怎么写"，不是"必须有哪些页"。

页面纪律（防漂移、防膨胀）：
1. **每条结论带出处**（日期 + day_seq，或 transcript 文件引用）——dreaming 校验的前提；
2. 页间用相对链接互引，index 是入口；
3. 冲突以新覆旧，但保留出处；低置信度结论显式标注，夜里优先回查原文核实；
4. **每夜必须做合并/归档**：重复页合并、过期页归档（移出 index 地图），防止 wiki 比流水还大；
5. **threads 是一等公民**：单会话助理折叠后不"断片"全靠它——未完成事项必须显式存活在注入层
   （注入层只读最新快照的 open 条目，见 §6.1）。

### 6.2 写权限矩阵【已定：用户 2026-11-21】

| 文件 | 写方 | 读方 |
|---|---|---|
| `router/notes/<日期>.md` | router（fs Write/Edit）、用户 | 整理 worker（只读）、router、用户 |
| `router/transcript/<日期>.jsonl` | 系统（中间件自动记录） | 整理 worker（只读）、router（ReadRecords）、控制台 |
| `router/ledger/`（turns/inbox） | router 记账 | 控制台（`/api/turns`、`/api/inbox`） |
| `router/memory/`（index/threads/其余 wiki 页/journal） | **仅记忆整理 worker** | router（只读）、控制台、用户 |

强制力【已定：用户 2026-11-21；同日二次修订：**统一靠提示词约定，不做硬限制**；
同日三次确认：**不改任何固定代码去限制 router**，权限已实际放宽】：
- 矩阵是**约定**，不是沙箱：router 侧"只写 notes"、worker 侧"只写 memory、不碰 notes / 素材"
  都写进各自的系统提示词；**不在代码工具或 fs 工具里做隔离**（不设专属 write_roots、不加额外校验）。
- 已生效：`.env` 配 `PLAY_ROOT=D:\`（2026-11-21），memory 区（`DATA_DIR/router/*`）整个落在
  fs 合法写路径内，"只约定、不强制"无需任何代码改动即成立。
- 副作用同样靠纪律：仓库目录 `D:\personal_agent` 物理上也在可写范围内，约定上 router/worker 不改仓库代码。
- router 与 worker 在合法路径范围内（禁区之外）权限一致，与 general worker 相同；
  白天 router 不改记忆、夜里 dreaming 的校验有可信基线，靠的是纪律。
用户物理上当然能改任何本地文件，但约定上只有 notes 是人机留言板。

### 6.1 threads 按天快照【已定方向：用户 2026-11-19】

用户方向：threads 按天记录，方便对比不同天的差异、追踪事项演变。具体化【建议】：

- **一天一份** `threads/threads-YYYY-MM-DD.md`，由夜间 dreaming 写入：
  承接昨日快照的全部条目 → 应用当日变更（新增 / 推进 / 关闭）→ 文件头部写一节
  **「今日变更」**（新增了什么、哪条推进到哪、哪条关闭及原因）。
- **条目结构**：稳定 `id`（首次出现时生成，之后不变）+ 标题 + 状态（open/done/dropped）
  + `first_seen` / `updated` 日期 + 一行说明。措辞可以改，id 不变——跨天追踪靠 id，不靠文本匹配。
- **最新一份 = 当前状态**：注入层与 router（通用 Read）只读最新文件；
  历史文件天然就是 markdown diff 素材（人看 / 程序 diff / dreaming 复盘都直接用）。
- **无变更也写快照**（原样承接 + 「今日变更：无」），保证"最新 = 当前"恒成立，
   diff 序列不断档。
- 概念划分：**threads 是"状态追踪"→ 按天分区、快照式**；wiki 主题页是"提炼后的知识"→
  当前态文档 + 出处。两种组织形式不混用：画像/偏好不需要每日快照，事项追踪不需要"当前态单页"。
- 体量与保留：单份几 KB，按天累积很小；长期保留还是按月归档【待讨论，见 §11】。

## 7. 夜间记忆 worker：与普通 worker 的差异【已定方向 + 建议】

`task_kind="memory_consolidate"` 已存在于契约 TaskKind。差异点：

**工具集【已定方向：用户 2026-11-21】：不造专属工具，也不做额外限制——与 general worker 同一套
通用 fs + Shell（代码），只要在合法路径范围内（禁区之外），worker 不受限**。
`agents/task_kinds.py` 注册表的 `_memory_tools` 直接复用 general 的装配，差异只剩提示词与默认 cwd；
当年"通用 fs 够不到 memory 区"的问题已随 `PLAY_ROOT=D:\`（2026-11-21 已生效）消失，
不需要为记忆 worker 单设 roots。
- Read 原生支持 `offset/limit` 行分页 → transcript 流式分批读不需要专属工具；
  Glob/Grep 承担"列页面 / 关键词找页"；
- 原子写 / diff / 统计 / 批量合并用 Shell 跑 python 代码；
- scratchpad 就是 `memory/scratch/` 普通目录，完成后清理；
- 完成报告 = `journal/<日期>.md` 文件 + `worker_result` 摘要，不需要 WriteReport；
- "记忆 worker 是 memory 区唯一写方"（§6.2）是**提示词约定**，不是沙箱强制——
  worker 在合法路径内自由，靠纪律不碰 notes / 素材区。
不装 CUA / 视觉工具（整理用不到）。

**大量读入 → 上下文压力的问题**【已定：用户 2026-11-21】：**记忆 worker 的上下文预算直接提到 50 万 tokens**
（DeepSeek 支持百万窗口），配合已有的 WorkerCompactMiddleware 三级压缩兜底，容量够用；
**工作流程（三遍 dreaming、分批节奏、scratchpad 纪律、游标续整）写进它自己的专属提示词**
（`memory_worker_prompt`），不靠通用提示词顺带。下面的工作模式作为配套纪律保留【建议】：
1. **流式分批**：一次只处理一批 transcript（如 100 条消息），提取该批候选笔记后即进入下一批，
   不在上下文里囤积原文；
2. **工作记忆外置**：中间产物（每批提取的候选条目）写到磁盘 scratchpad（`memory/scratch/<day>/`），
   合并阶段从磁盘重读——**即使上下文被折叠，进度也不丢**，折叠对记忆 worker 无害化；
3. **按 task_kind 差异化中间件参数**：50w 预算只给记忆 worker（装配层按 kind 传参，
   不动通用 worker 默认值）；可顺带放宽 keep_recent_tokens、关闭视觉中间件；
4. **游标持久化**：day + 批游标 + covers 落盘，崩溃后从断点续整（与 XAUTOCLAIM 的恢复语义一致）。

**三遍 dreaming**（单 worker 内分阶段，先不拆多 worker【建议】）：
- Pass1 提取：分批读 transcript + notes → 候选条目（全部带出处）写 scratchpad；
- Pass2 校验整合：对照现有 wiki——纠错（低置信条目回查原文）、合并重复、更新过时结论、
  更新/新建主题页、归档膨胀页；
- Pass3 重建注入层：重写 index.md + 写当日 threads 快照（§6.1），写报告。

**与 router 的交接**：worker 不写 router 流水（进程边界不变）；报告经 `worker_result` 回 router，
router 记账（沿用 consolidated 记账思想）并触发上下文更新（§8）。整理过程中 router 无需醒着（§9）。

## 8. Router 上下文更新与 new_context【已定方向 + 建议；2026-11-21 用户提出新想法，待讨论】

- `new_context` 提供给 router【已定】，语义修订为**主动上下文管理**：
  "把当前上下文折叠到安全边界，注入最新 index.md + threads + 热点事实"。
  机件可复用：`ContextBudgetMiddleware.wrap_tool_call` 已拦截 `new_context` 置 force-fold 信号，
  只是工具未暴露；重新暴露并接上"折叠 + 注入最新记忆"的替换路径即可，折叠算法（成对边界、
  保留最后 HumanMessage）不变。
- 夜间整理完成后的上下文更新【建议：**懒折叠**】：router 收到记忆 worker 的 `worker_result` 时
  只记账 + 置"记忆已更新"脏标记；**下一次被唤醒**（用户消息/定时/worker 事件）时在 `before_model`
  执行折叠 + 注入。避免为了折叠专门唤醒一次 router，与"降低 router 唤醒必要性"的方向一致。
- 白天被动折叠（soft/hard 预算）保留为安全网；换大窗口模型时预算经 `.env`
  （`CONTEXT_SOFT_TOKENS` / `CONTEXT_HARD_TOKENS`）调整，**不改冻结的 config.py**。
- worker 上下文机制不变【已定】。

### 8.1 2026-11-21 用户新想法与方案对比【已定稿：演化为 §8.2 时间戳折叠】

用户想法：**A.** 每天 04:00 自动更新 router 上下文（注入 index + 最近 3 天 threads），
相应**删除 new_context 工具**——代价是对 threads/index 质量要求变高；
**B.** 记忆 worker 直接把 router 的 checkpoint messages 拉下来、替它整理重写上下文
（类似 Meta Superintelligence Lab 的 context-language-model 做法）。

| 方案 | 机制 | 优点 | 风险/成本 |
|---|---|---|---|
| 1. 懒折叠（原建议） | worker 只更文件+脏标记，router 下次唤醒时 before_model 折叠 | 最简单，写权不出 router | 折叠内容由 router 当场算，夜间 worker 的全局视野用不上 |
| 2. 定时刷新（用户 A） | 04:00 折叠+注入，删 new_context | 早晨上下文确定干净 | 需处理"谁来执行折叠"（见方案 4 的零成本应用方式） |
| 3. worker 直改 checkpoint（用户 B） | worker `get_tuple` 读 router:main → 重写 → `put` 写回 | 夜间全局视野直接塑形上下文；`put` 是追加新版本，改坏可删版本回滚 | ①竞态：凌晨消息唤醒 router 撞上编辑（需 Redis 锁 + supervisor 拉起前检查；事件留在 stream 不丢）②切点须保证 tool_calls 成对（复用折叠纯函数）③深耦合 LangGraph checkpoint 内部格式 |
| **4. 折中：worker 算、router 应用【助手推荐】** | worker 夜里**只读** checkpoint，产出**折叠计划**（切点+handoff+注入内容）写盘；router 每次**启动时**检查计划，`graph.update_state` 应用后再消费事件 | 效果=方案 3（醒来上下文已整理好）；`update_state` 不走模型、零 LLM 成本；checkpoint 写权不出 router 自己的 graph；无竞态、无格式 hacking | 计划文件格式需定义（简单 JSON：保留消息 id 列表/切点 + 注入文本） |

- 方案 2 的"04:00 生效"不必专门唤醒 router 跑模型轮：计划在**下一次自然唤醒**时零成本应用；
  若要求 04:00 准时生效，投一条 system 事件唤醒一次即可（应用本身不花 tokens）。
- **new_context 直接删除**【已定：用户 2026-11-21】：只删 router 侧（含 ContextBudgetMiddleware
  的 force-fold 拦截路径）；worker 侧 `tools/context_tools.py` 不动（worker 上下文机制不变）。
- 注入内容 = index.md + **最近 3 天 threads 快照**【用户方向 2026-11-21】；
  对 threads/index 的质量要求由 dreaming Pass3 校验兜底（最新快照必须完整覆盖 open 事项）。

### 8.2 定稿：时间戳折叠（方案 4 的修订版）【已定：用户 2026-11-21】

用户对"折叠计划"的切点判断提出疑虑（凌晨 2 点的问答带 tool_calls，智能切点可能把
tool_call/tool_message 配对切坏），并给出更简单的规则——**不做智能切点，纯按时间戳**：

1. **每条 message 增加 `created_at`**（Asia/Shanghai ISO8601）。实现【建议】：
   router 侧 middleware 在每次 `before_model` 给 state 中缺 `created_at` 的消息补
   `additional_kwargs["created_at"]`（此时 Human/AI/Tool 消息都已进 state，一个钩子全覆盖；
   additional_kwargs 随 checkpoint 持久化）；
2. **折叠规则**：删除 `created_at` 早于**今天 00:00** 的全部消息；**00:00 之后的消息全量保留**
   ——它们属于"新的一天"，还没被昨夜 dreaming 消化（§11-7 日界），且通常没几条，全留不贵。
   凌晨 2 点的问答（含成对 tool_calls）天然整体落在保留侧，配对不会被切；
3. **机械 pair-snap 兜底**【建议】：唯一理论边界是某对 tool_call/ToolMessage 恰好跨 0 点
   （工具往返秒级，几乎不可能）。防御规则一行：若保留侧第一条是 ToolMessage，
   把切点前移到它的 AIMessage（多保一对，无害）；
4. **注入**：折叠的同时注入 SystemMessage = handoff 文本（夜间 worker 产出）+ index.md 全文
   + 最近 3 天 threads 快照；
5. **执行者与成本**：沿用方案 4 的"worker 算、router 应用"骨架，但规则确定化后 worker 要算的
   只剩 handoff 文本（index/threads 本来就是它写的文件）；router **启动时**发现"昨夜整理成功"
   标记即机械应用（`graph.update_state`，不走模型、零 LLM 成本），然后才开始消费事件。
   **前置条件**：昨夜 dreaming 成功才折叠（有标记才动上下文）；失败/未跑则保持原样，
   等被动折叠安全网兜底；
6. **预算配套**【已定，已落 .env】：白天不再对 router 上下文做任何主动裁剪，
   `CONTEXT_SOFT_TOKENS=600000` / `CONTEXT_HARD_TOKENS=800000`（模型百万窗口），
   被动折叠只剩安全网作用；不改冻结的 config.py。

## 9. 定时任务：按派发目标分两类【已定方向 + 用户补充 2026-11-21 + 契约影响】

现状：`ScheduleScanner` 只投 `timer` 事件 → 唤醒 router → router 派 worker（每次定时都消耗 router
 tokens 并污染其上下文）。

目标：定时任务按**派发目标**分两大类——不是所有活都该 worker 干，有些活本来就要发给 router。

**A. router 类（保留现有 timer 路径，零改动）**：
- 提醒用户某件事（timer → router → ReplyUser 发飞书）；
- 提醒 router 更新记忆 / 整理上下文（router 写当日笔记，或触发 new_context / 派整理 worker）；
- 需要判断、措辞、会话上下文的事（早报、汇总等；router 想转手时仍可再派 worker）。
这类活消耗 router 上下文是**应该的**——直派要省的是"机械活不必过脑子"，不是"永远不唤醒 router"。
router 永远是同一线程 `router:main`，无 fresh/resume 之分。

**B. worker 类（scanner 到期直接拉起 worker_runner，不经过 router）**：
- 两种模式：
  - **fresh**：每次从 0 开始，thread = `schedule:<id>:<run_ulid>`；
  - **resume**：每次接着上次，thread = `schedule:<id>`，checkpoint 保留 **7 天**后删除（既有规则；
    目前仅在 docs 中见到"general 7 天策略"的表述，实现位置待确认【待讨论】）；
- router 只在**任务完成**（worker_result）或 **worker 有疑问需要指示**时被唤醒。

机制盘点（好消息：事件层基本不用动）：
- 疑问通道可用**现有** `worker_message` / `worker_notice` 事件类型 + `SessionStatus.waiting_router`，
  **不需要新增 EVENT_TYPES**（contracts.py 冻结）；
- schedule 表需加字段：`dispatch_target(router|worker)`（**默认 router，存量行向后兼容**）、
  `mode(fresh|resume)`（仅 worker 类有意义）、`task_kind`、`prompt`
  （store/sqlite.py 非冻结，但表结构变更应记入契约）；
- 工具层【已定：用户 2026-11-21】：`Schedule` 工具（tools/schedule.py）增加参数
  `dispatch_target`（默认 router）与 `mode`（默认 fresh，仅 worker 类生效）——
  **派哪类、用哪种模式，由 router 创建任务时自己选择**，scanner 只按行执行、不做判断；
- 提醒类如需固定发到某个会话（而非 last_chat_id），schedule 行可加可选 `chat_id`【待讨论，小点】；
- 夜间记忆整理本身就是一条 worker 类定时任务（整理进度靠磁盘游标续接，thread 用 fresh 即可【建议】）。

**契约影响（需走 ENGINEERING_CONTRACT §11 变更流程）【待讨论，关键决策】**：
SQLite 业务表目前"只由 router 写"。gateway 直接派 worker 时 sessions 行谁来建？

**现状核实（2026-11-21）**：sessions 行 = SQLite `sessions` 表里每个 worker 任务的一行
（`id=worker:<ulid>`、title、status、cwd、pid、pid_started_at、created_at、first_message_at），
是控制台 Workers 页、router 的 list_sessions/TaskOutput、gateway 看门狗（pid+checkpoint 停滞巡检）
的共同锚点。今天的写方：router `SessionService.create_session` upsert 行 + set_pid
（tools/sessions.py:198/246）；**worker 退出时已在写自己的 status/first_message_at**
（worker_runner.py:254-255，既成事实）；router 收 worker_result 再更新（main_router.py:149）。
直派场景下没人建行 → 控制台看不见该 worker、看门狗无行可巡。两个选项：
- A. gateway 写 sessions 行 → 打破现有写权边界；
- B. **worker 启动时自登记自己的 session 行**（TaskSpec 里有 title/cwd，pid 自己知道，
  upsert 幂等），gateway 只负责拉起进程 + 传参，router 只在收到 result 时更新状态
  → 边界修订为"router + worker（仅自己的行）"。
助手推荐 B：只是把 worker 已写 status 的既成事实往前延伸半步，gateway 保持零 SQLite 写。
**（已定：用户 2026-11-21 选 B。契约修订措辞落实施期：§5.2 写权边界改为
"router + worker（仅自己的 session 行）"，gateway 不写 SQLite 不变。）**

### 9.1 checkpoint 保留与清理策略【已定方向：用户 2026-11-21 + 建议】

现状核实（2026-11-21）：**代码中没有任何 checkpoint 清理实现**——"general 7 天策略"只在
docs 出现过，从未落地（原开放问题 #10 的答案）。PG public 下只有 LangGraph 标准四张表
（checkpoint_migrations / checkpoints / checkpoint_blobs / checkpoint_writes）；
LangGraph 每个 super-step 写一个 checkpoint 版本（time travel 用），thread 会积累大量版本，
旧版本 blobs 里还带着折叠前的大 messages——**膨胀主因**。

用户方向：
1. **版本裁剪**：每个 worker thread 最多保留最新 **7 个** checkpoint 版本（worker 不需要时光
   回溯，可以更少）；
2. **resume 任务的 thread 不删**，除非该 schedule 已停用（cancelled/paused）**且**满 7 天。

助手补充细节【建议】：
- 版本裁剪只删旧版本、**必须保留最新**（resume/get_tuple/fork_session 都只读最新，裁剪不影响
  任何现有功能）；删除范围 = checkpoints + checkpoint_blobs + checkpoint_writes 三表旧行；
- **router:main 的 thread 同样参与版本裁剪**（它每轮 invoke 写多个版本，是 blobs 膨胀的主要
  来源；单会话助理不需要对自己 time travel）；
- fresh 一次性任务的 thread：done 后满 7 天整 thread 清理（结论已落 result.json / turns 记账）；
- 执行者【已定：用户 2026-11-21】：**专门的清理 worker，每周一 05:00**（cron `0 5 * * 1`，
  Asia/Shanghai，worker 类直派定时任务、fresh 模式）；SQL 清理脚本放 `scripts/`，
  worker 用 Shell 跑——gateway 零改动、不引入 PG 写权。

过渡方案【已落地 2026-11-22】：直派能力已实现（`schedules.dispatch="worker"`，gateway 的
`ScheduleScanner` 到点直接 `create_session(task_kind=...)`，不经过 router；worker 完成后
`worker_result` 照常回到 router 记账）。夜间记忆整理已切到这条路：
`cron="0 4 * * *"`、`dispatch="worker"`、`task_kind="memory_consolidate"`、
`title="记忆整理 {yesterday}"`，prompt 里的 `{yesterday}` 由触发时渲染成上海日期。
原 `ConsolidateMemory` 工具（router 侧包装）随之删除——它只是 `create_session` 的一层
日期/路径打包，而素材路径与产出契约本来就完整写在 `memory_worker_prompt` 里。

## 10. 分期建议【待讨论】

- **P1（最小闭环，尽量零契约变更）**：transcript 按天 journal + 每日笔记约定 + 夜间 worker 最小版
  （单遍整理 → index/threads 雏形）+ new_context 修订；触发暂走现有 timer 路径。
- **P2**：wiki 分页 + 三遍 dreaming + 懒折叠；定时直派 worker（含 §9 契约变更、7 天清理核实）。
- **P3**：打磨——失败任务复盘纳入 dreaming、归档策略、控制台记忆观测页、占位代码清理/迁移。

## 11. 开放问题清单（2026-11-21 用户逐条表态后更新）

1. （已解决 2026-11-21）transcript 记**全量 messages**（含 tool_calls/工具结果）——
   用户："肯定是记全量，不会有太多的，包含所有的 router 上下文"。
2. （已解决 2026-11-21）占位代码处置：turns/inbox 保留给 UI 并迁 `router/ledger/`（§4）；
   **facts.jsonl / decisions.jsonl / summary.md 直接废弃**，不迁移；清理动作归入实施期。
3. （已解决 2026-11-21）wiki 只必备 index.md + threads/，其余页面由整理 worker 运行时自由创建，不预设页名。
4. （已解决 2026-11-21）记忆 worker 预算提到 **50w tokens**（DeepSeek 百万窗口），工作流写进专属提示词，
   已有压缩机制兜底；流式 + scratchpad 作为配套纪律保留（§7）。
5. （已解决 2026-11-21，定稿见 §8.2）**时间戳折叠**：new_context 直接删除（router 侧）；
   每条 message 加 `created_at`；折叠 = 删今天 00:00 之前的全部消息 + 注入 handoff/index/
   最近 3 天 threads，00:00 之后全量保留（属新一天、未被昨夜消化，量小）；凌晨带 tool_calls
   的问答天然整体落在保留侧，配对不会被切坏；router 预算放大到 80w（已落 .env），
   白天不做任何主动裁剪，被动折叠只剩安全网。
6. （已解决 2026-11-21）**选 B**：worker 启动时自登记自己的 sessions 行；
   契约写权边界修订为"router + worker（仅自己的行）"，gateway 保持零 SQLite 写（§9）。
7. （已解决 2026-11-21）夜间时刻与日界：**04:00 整理上一自然日**（Asia/Shanghai）；
   当天 0 点以后的消息属于当天的 transcript 文件，明晚再整理——用户："明确边界，
   不需要管 0 点以后的，避免混了"。
8. （已解决 2026-11-21）dreaming **纳入**当天失败 worker 的复盘（策略纠错，不只是事实纠错）；
   素材建议 = `workers/<task_id>/result.json`（失败原因）+ router transcript 中该任务的
   派发/结果片段，不读 worker checkpoint（会被 §9.1 清理）。
9. （已解决 2026-11-21，详见 §9.1）resume 任务的 thread **不删**，除非 schedule 已停用且满 7 天；
   版本层面每个 thread 最多保留最新 7 个 checkpoint（worker 不需要时光回溯，可更少）。
   "降级 fresh 还是报错"的问题随之消失——最新版恒在。
10. （已解决 2026-11-21）核实结论：**7 天清理从未实现**（仅 docs 表述）；按 §9.1 新建，
    执行者已定：专门清理 worker，每周一 05:00（cron `0 5 * * 1`，worker 类直派、fresh）。
11. （已解决 2026-11-21）threads 每日快照**长期保留**（体量小，不做归档）。
12. （已解决 2026-11-21，同日二次修订）笔记放 `router/notes/`（memory 区外）；
    "router 只写 notes、worker 只写 memory"**统一为提示词约定，不做工具级硬限制**（§5 / §6.2）；
    同日三次确认：`PLAY_ROOT=D:\` 已生效，memory 区物理可达，零代码改动。

## 12. 参考资料

- Anthropic, *Components of agent memory*: https://www.anthropic.com/research/components-of-agent-memory
- ReMe（AgentScope, Alibaba）: https://github.com/agentscope-ai/ReMe
  （文件即记忆 / 日记 vs 长期知识 / 段落检索；本设计借结构不引依赖）
- 本仓库相关现状：`agents/middleware.py`（ContextBudgetMiddleware / force-fold）、
  `agents/context_budget.py`（折叠纯函数）、`memory/jsonl.py`（append+fsync 模式）、
  `tools/schedule.py` + `main_gateway.py::ScheduleScanner`（timer 触发）、
  `contracts.py`（EVENT_TYPES / TaskKind / SessionStatus）、
  `docs/CUA_BROWSER_RESEARCH.md`（提及 general 7 天 checkpoint 策略）。