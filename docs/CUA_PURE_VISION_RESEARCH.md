# 纯视觉 CUA 选型：Anthropic vs OpenAI

> 目的：为"同一套「截图 + 坐标动作」循环，先从 browser 扩到 computer"的路线做选型依据。
> 日期：2026-11-13。本文只做调研与设计，**不改代码、不提交 commit**。
>
> 证据标记：
> - **【官方】** 厂商文档原文（附链接与章节名）
> - **【官方代码】** 厂商参考实现源码（附 raw 链接与行号）
> - **【实测】** 2026-11-13 在本机、当前模型 `GLM-5.3-Flash` 上实测，附产物路径
> - **【项目代码】** 本仓库实现，附 `文件:行号`
> - **【计算】** 由上述事实推出的算式，给出每一步
> - **【未验证】** 没有一手证据，本文不当结论用

---

## 0. 结论先行

**一句话建议：纯视觉路线可以立项，但必须先过"坐标命中率"闸门——用现成 Chromium 158 + 当前模型做一次 grounding 基准（1280×800、30 个点击目标）；命中率 ≥90% 才把纯视觉当主路径，否则走"视觉定位 + DOM 吸附/ref 校验"的混合方案。**

落地顺序（每步都有明确验收物）：

| 步骤 | 做什么 | 验收物 |
|---|---|---|
| 1 | **坐标命中率基准**：3 类页面 × 10 个目标，模型给坐标，脚本 `elementFromPoint` 判定命中 | 命中率数字 + 失败样本截图 |
| 2 | **最小闭环**：`screenshot → click → screenshot`，单动作单拍，25 步硬上限 | 一条能跑完的真实任务录像/日志 |
| 3 | **补齐动作原语**：type / key / scroll / drag / wait，统一 `button`+`count` 参数 | 6 个工具的 schema 与单测 |
| 4 | **失败恢复**：坐标 clamp、截图哈希无变化提示、同点三连击熔断、步数上限 | 注入故障后能自恢复的用例 |

**不要一开始就做**：多显示器、浏览器 chrome（地址栏/标签页）操作、跨应用桌面控制。这三块在两家官方方案里都属于"自己实现、没有标准答案"的部分（见 §1.6、§2.5）。

---

> **2026-11-13 追加**：两家"页面级寻址（browser use）"路线的补充调研见 **§7**，其中 §7.3 是纯视觉 / 页面级 / 我们的 ref 快照三路线对照表。

> **2026-11-14 定稿**：选型已定，见 **§8**。§0 与 §5 的"一句话建议"以 §8.0 为准；§7.4 末尾关于"两个 task_kind 分流"的建议被 **§8.7** 取代（原文保留以便追溯）。

---

## 1. Anthropic（Claude computer use）

### 1.1 工具形态：两代并存

| 代际 | 声明方式 | 坐标参数 | 状态 |
|---|---|---|---|
| 当前 | `{"type": "computer_toolset_20260801"}`，**一个 toolset 展开 17 个成员工具** | 无 `display_*` 参数；坐标一律是**你回传的截图像素空间** | 无 beta header；Claude 4.7+/5.x 系列【官方】 |
| 上一代 | `{"type": "computer_20251124", "name": "computer", "display_width_px", "display_height_px", "display_number"}` | 由 `display_*` 声明屏幕尺寸 | 需要 beta header；Opus 4.5/4.6/4.7、Sonnet 4.6 等【官方】 |
| 更早 | `computer_20250124`（`computer-use-2025-01-24`）、`computer_20241022` | 同上 | 旧模型【官方】 |

> 出处：`https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool`。
> 注意：`docs.claude.com` 同路径的页面是**旧版**（只有 `computer_20250124`，推荐分辨率 ≤1280×800），两个域名内容不同步；本文以 `platform.claude.com` 为准，旧版差异单独标注。

**这是本次调研最重要的一条**：Anthropic 已经把"告诉模型屏幕尺寸"这件事**从参数里删掉了**。当前 toolset 明确拒绝 `display_width_px` / `display_height_px` / `display_number`，文档原文：

> "`display_width_px`, `display_height_px`, and `display_number`: coordinates are always in the pixel space of the screenshots you return."
> —— 【官方】Tool parameters 节

也就是说，"模型猜分辨率"的问题在新版里通过**改坐标系语义**解决了，而不是靠提示词叮嘱。

### 1.2 动作空间与参数结构

17 个成员工具（【官方】Available actions 节，逐字整理）：

| 成员 | 输入 | 说明 |
|---|---|---|
| `screenshot` | `{}` | 全屏截图，以 image 块返回 |
| `zoom` | `region: [x0,y0,x1,y1]` | 只截该区域、**按全分辨率**返回，再缩放到常规截图尺寸；用于读小字 |
| `left_click` | `coordinate?`, `text?`（修饰键：`shift`/`ctrl`/`alt`/`super`/`ctrl+shift`） | 省略 `coordinate` 就在当前光标处点击 |
| `right_click` / `middle_click` / `double_click` / `triple_click` | 同 `left_click` |  |
| `left_click_drag` | `start_coordinate`, `coordinate`, `text?` | 按下—拖—松开 |
| `mouse_move` | `coordinate` | 只移动，可用于 hover |
| `left_mouse_down` / `left_mouse_up` | `{}` | 在**当前光标位置**按下/松开，用于 `left_click_drag` 表达不了的拖拽 |
| `cursor_position` | `{}` | 以文本返回 `[x,y]` |
| `scroll` | `scroll_direction`（up/down/left/right）、`scroll_amount`（滚轮格数）、`coordinate?`、`text?` | 在指定点或当前光标处滚 |
| `type` | `text` | 在当前焦点键入字面文本 |
| `key` | `text`（如 `"Return"`、`"ctrl+s"`）、`repeat?`（1–100） | 按键/组合键 |
| `hold_key` | `text`, `duration`（秒，≤300） | 按住 |
| `wait` | `duration`（秒，≤300） | 等待 |

三个实现要点（【官方】同节）：

1. 调用是 `tool_use` 块，`name` 是成员名，并带 `"toolset_name": "computer"`；**没有 `action` 字段**，`input` 里只有该成员的参数。
2. 所有成员默认开启（含 `zoom`）。环境做不了的成员，用 `configs: {"zoom": {"enabled": false}}` 关掉，**不要**留着然后返回错误。
3. 分发必须按 `(toolset_name, name)` 组合判断——用户自定义工具可以重名。

### 1.3 坐标约定与缩放（核心）

**规则（【官方】Available actions 节首条）：**

> "Coordinates are in screenshot pixels. Every `coordinate`, `start_coordinate`, and `region` value, and the position that `cursor_position` reports, is in the pixel space of the full-display screenshots you return, with the origin at the top left. Zoom images don't change this: after a `zoom`, Claude still expresses coordinates in the full screenshot's space, never relative to the zoomed image."
> "If you scale screenshots down before returning them, scale Claude's coordinates back up before applying them to the real display."

翻译成工程语言：

- **模型只认"你给它的那张图"的像素**，与物理屏幕分辨率无关。
- **你缩图，就必须自己乘回去**；模型不会替你换算，也不该让它换算。
- `zoom` 只改变**感知分辨率**，不改变**坐标系**——这点很容易写错，zoom 后仍按全图坐标点击。

**你问的具体场景：物理屏 2560×1440、截图 1280×720。**
模型输出的 `(x, y)` ∈ [0,1279]×[0,719]；落到真实屏幕要 `×2`。Anthropic 参考实现就是这么做的：

```python
# anthropic-quickstarts / computer-use-demo / computer_use_demo/tools/computer.py
# 【官方代码】L37-43, L233-256
MAX_SCALING_TARGETS = {
    "XGA":   (1024, 768),   # 4:3
    "WXGA":  (1280, 800),   # 16:10
    "FWXGA": (1366, 768),   # ~16:9
}
# 注释原文：sizes above XGA/WXGA are not recommended
#   scale down to one of these targets if ComputerTool._scaling_enabled is set
...
x_scaling_factor = target.width / self.width      # < 1
if source == ScalingSource.API:                    # 模型给的坐标 → 真实屏幕
    if x > self.width or y > self.height:
        raise ToolError(f"Coordinates {x}, {y} are out of bounds")
    return round(x / x_scaling_factor), round(y / y_scaling_factor)
return round(x * x_scaling_factor), round(y * y_scaling_factor)  # 反向
```

可提炼的三条策略：

1. **按宽高比匹配目标档位**（容差 0.02），只往小缩、不放大；不匹配就不缩放。
2. **越界坐标直接报错**，不静默 clamp——让模型看到 `is_error` 才能纠正。
3. 截图缩放和坐标换算用**同一个** factor，成对出现，避免出现"图缩了、坐标没缩"的半套实现。

**推荐分辨率。**
- 旧版文档（`docs.claude.com` 中文页，Tool parameters 节）："为了获得最佳性能，请将显示分辨率保持在 **1280x800（WXGA）或以下**。更高的分辨率可能由于图像调整大小而导致准确性问题。"【官方】
- 当前版本（`platform.claude.com`）改为按"图像 token 上限"约束，见下。
- 检索摘要还出现过 "桌面任务 1024×768 / 1280×720，Web 应用 1280×800 / 1366×768" 的说法，**未逐字核对到原文**，标【未验证】。

**图像上限（决定了截图该多大）**【官方】Vision 文档 Resolution and token cost 节：

| 档位 | 模型 | 长边上限 | 视觉 token 上限 |
|---|---|---|---|
| High-resolution | Claude 4.7+ | 2,576 px | 4,784 |
| Standard | 其它 | 1,568 px | 1,568 |

- 计费公式：`⌈width/28⌉ × ⌈height/28⌉` 个视觉 token。
- **关键差异**：普通图片超限会被自动缩小；但 **`tool_result` 里的截图/zoom 图超限会直接报 validation error，不帮你缩**——"resize those images in your application before returning them"【官方】。所以缩放是**调用方的强制义务**，不是优化项。

按公式算几个常用尺寸【计算】：

| 截图尺寸 | 视觉 token |
|---|---|
| 1024×768 | 37×28 = **1,036** |
| 1280×720 | 46×26 = **1,196** |
| 1280×800 | 46×29 = **1,334** |
| 1366×768 | 49×28 = **1,372** |
| 1920×1080 | 超 1,568 上限 → 缩到 1456×819 = **1,560**（官方表的实测值） |

### 1.4 循环结构

- **谁截图**：截图是模型主动发起的 `screenshot` / `zoom` 成员调用；**执行方是调用方**（"The computer use tool must be explicitly executed by your application"）。
- **怎么回灌**：一条 `user` 消息，里面每个 `tool_use` 对应一个 `tool_result`；**`screenshot` 与 `zoom` 的 `content` 是 image 块**（base64 PNG），其它动作回 `"OK"` 文本即可【官方】How computer use works / Batch actions 节，含完整 JSON 样例。
- **一步一拍 vs 连发**：**两者都支持**。模型可以在**一次响应里**返回多个成员调用（batch action），调用方**按顺序执行**；任一步失败就**停止后续**，并把剩下的都回成 `is_error: true` + 固定文案 `Not executed: an earlier computer action in this turn failed.`；**每个 `tool_use` 都必须有对应的 `tool_result`**，否则下一次请求 `invalid_request_error`。【官方】Batch actions 节
- 官方还给了省 round trip 的技巧：模型通常以 `screenshot` 结尾；**如果 batch 没以 screenshot 结尾，调用方可以在最后一个 result 上附带一张当前截图**，模型就能立刻看到结果【官方】。
- 如果引擎只能一轮一动作：`tool_choice.disable_parallel_tool_use = true`，模型每轮最多返回一个成员【官方】Tool parameters 节。

### 1.5 提示词层面的官方要求

【官方】Optimize model performance with prompting 节，7 条，逐条对应：

| # | 官方要求 | 解决的问题 |
|---|---|---|
| 1 | 任务要小、每步给明确指令 | 长任务跑偏 |
| 2 | 要求每步后截图并显式评估："After each step, take a screenshot and carefully evaluate if you have achieved the right outcome. State in one sentence what the screenshot shows and whether the step succeeded." | **模型假设动作成功**（最常见失败） |
| 3 | 下拉框、滚动条不好用鼠标操作 → 提示改用键盘快捷键 | 细粒度鼠标操作不可靠 |
| 4 | 可重复任务在 prompt 里放成功示例截图 + 工具调用 | 少样本对齐 |
| 5 | 需要登录时把凭据放 `<robot_credentials>`，并先读防注入指南 | 凭据与注入风险 |
| 6 | **同一轮里，指令文本放在截图之前**——"Providing the target description before the image is processed improves click accuracy." | 提升点击精度 |
| 7 | 小字（文件名、标签、状态栏、行号）用 `zoom`；模型不 zoom 时要**点名具体区域**，不要问"整个屏幕" | 分辨率不足 |
| 8 | 想让每个 batch 以截图收尾，就在 system prompt 里写死 | 观测缺失 |

另外系统提示会自动生成，开头是：

> "You have access to a set of functions you can use to answer the user's question. This includes access to a sandboxed computing environment. You do NOT currently have the ability to inspect files or interact with external resources, except by invoking the below functions."

最后两条是**新一代才有的**（指令先于图、zoom 读小字），旧版文档里没有。

### 1.6 已知失败模式与官方缓解

| 失败模式 | 官方是否明说 | 官方缓解 |
|---|---|---|
| 模型假设动作成功、不检查结果 | 是（"Claude sometimes acts as if its actions succeeded without explicitly checking their results"） | 提示词强制每步截图 + 一句话评估 |
| 下拉框/滚动条鼠标操作困难 | 是 | 改用键盘快捷键 |
| 小字看不清 | 是 | `zoom` 成员；提示点名区域 |
| 死循环 / 失控成本 | 是 | 应用层 `max_iterations`（官方示例默认 10）+ 所有 batch 必须有对应 result |
| 坐标越界 | 是（参考实现） | 抛 `ToolError`，不静默 clamp |
| 多显示器 | **新 toolset 已移除 `display_number`**，一屏一套坐标 | 官方未给多屏方案【未验证】 |
| 系统 DPI 缩放 | 官方文档未直接覆盖 | 参考实现只在"截图尺寸 ≠ 屏幕尺寸"这一层处理，不处理 DPI【未验证】 |

> **关于"官方 limitations 章节"**：`platform.claude.com` 的 computer use 页在 `Understand computer use limitations` 一节之前被工具截断（单次抓取上限），该节正文**未获取**。上表只收录我能读到原文的条目，不用记忆补。
> 唯一读到的风险声明是 beta 期安全提示："may be susceptible to exploits and inadvertent mistakes … discourage trusting it in fully authenticated environments"（旧版 Overview 节）。

---

## 2. OpenAI

### 2.1 两条线：产品与 API

| | Operator / CUA（产品） | computer use tool（API） |
|---|---|---|
| 形态 | ChatGPT 里的托管产品（operator.chatgpt.com） | Responses API 里的一个工具 + 专用模型 |
| 模型 | CUA（GPT-4o 视觉 + RL 推理） | `computer-use-preview`（旧）→ `{"type":"computer"}` + `gpt-5.4`（新，微软侧文档） |
| 你能改什么 | 基本不能 | 环境、截图、动作执行、循环、安全确认全由你实现 |
| 动作回传 | 内部 | `computer_call` / `computer_call.actions[]` |

【官方】产品侧：`https://openai.com/index/computer-using-agent/`（2025-01-23）——"CUA processes raw pixel data to understand what's happening on the screen and uses a virtual mouse and keyboard… without needing specialized APIs"；OSWorld 38.1%、WebArena 58.1%、WebVoyager 87%。
**Operator 目前是否仍是独立入口，本文未验证**（产品侧变化快，不影响 API 选型）。

**一条硬约束**：computer use 只在 **Responses API** 提供，"It is not available on Chat Completions"【官方】。我们现在的模型调用走 Chat Completions 兼容网关，**接不了这个工具**——这不是能力问题，是接口形态问题。

### 2.2 动作空间

**旧一代（`computer_use_preview`，官方指南）**：`click(x,y,button)`、`scroll(x,y,scrollX,scrollY)`、`keypress(keys[])`、`type(text)`、`wait()`、`screenshot`。
**新一代（`{"type":"computer"}`，微软 Azure 官方文档的完整 handler）**【官方】`learn.microsoft.com/en-us/azure/foundry-classic/openai/how-to/computer-use`：

| action | 字段 | 备注 |
|---|---|---|
| `click` | `x`, `y`, `button`（left/right/middle/**back/forward/wheel**） | `back`/`forward` 直接映射浏览器前进后退；`wheel` 走 `mouse.wheel` |
| `double_click` | `x`, `y` | |
| `drag` | `path: [{x,y}, …]`（≥2 点） | 逐点 `mouse.move`，最后 `up` |
| `move` | `x`, `y` | 只移动 |
| `scroll` | `x`, `y`, `scroll_x`, `scroll_y` | 实现是 `window.scrollBy(..., behavior:'smooth')` |
| `keypress` | `keys[]` | 多键组合按序 down、逆序 up |
| `type` | `text` | `keyboard.type(text, delay=20)` |
| `wait` | `ms` | |
| `screenshot` | — | 无参数 |

坐标处理：`validate_coordinates` 把 x/y **clamp 到 `[0, DISPLAY_WIDTH/HEIGHT]`**，而不是报错【官方】。

### 2.3 循环结构：每次动作后必须回传新截图

【官方】指南 Integration 节 5 步：

1. 发请求，带 `computer_use_preview` 工具，声明 `display_width` / `display_height` / `environment`（`browser`/`mac`/`windows`/`ubuntu`），**必须** `truncation="auto"`；可附首帧截图。
2. 收 `computer_call`，`action` 里是建议动作；可能同时返回 `reasoning` 项（`summary` 可设 `concise`/`detailed`）。
3. 在你的环境执行动作。
4. **动作后必须重新截图**。
5. 把新截图作为 `computer_call_output` 发回去，循环。

新形态把"一次一动作"升级成"一次一批"：`computer_call.actions[]` 是**数组**，按序执行后回**一张**新截图【官方 Azure 文档】：

```json
{"type":"computer_call","call_id":"call_...","actions":[{"type":"click","button":"left","x":135,"y":193}],
 "pending_safety_checks":[...]}
// 回传
{"type":"computer_call_output","call_id":"call_...",
 "output":{"type":"computer_screenshot","image_url":"data:image/png;base64,...","detail":"original"}}
```

两个容易踩的点【官方】：

- 若不用 `previous_response_id`，**必须把上一轮输出里的 `reasoning` 项一并回传**，否则请求失败；`reasoning` 项只兼容产生它的模型。
- 安全校验通过 `pending_safety_checks` 下发，必须在下一轮用 `acknowledged_safety_checks` 确认才能继续。检查项：截图里的恶意指令、无关域名、敏感域名。

### 2.4 纯像素，还是像素 + 辅助信号？

**API 层：纯像素。** 证据是 schema 本身——回传通道只有 `computer_screenshot`（`image_url` + `detail`），**没有任何可以塞 DOM / a11y tree 的字段**；输入侧也只接受截图与文本【官方】。产品博客同样表述为"raw pixel data … without needing specialized APIs"。【官方】

**但有三处"不是纯像素"的东西，要说清在哪一层：**

1. **`environment` 先验**：`browser`/`mac`/`windows`/`ubuntu` 是文本侧提示，让模型知道自己在什么系统里，属于"先验注入"，不是感知。
2. **`detail: "original"`**：控制图像保真度（不降采样），属于图像侧参数。
3. **浏览器专用按钮**：`button: back/forward/wheel` 把"浏览器导航"编码进了动作空间——模型不需要点地址栏旁边的箭头，直接发 `click(button="back")`。这是**用动作空间偷换感知**，很聪明，但只对 `environment: "browser"` 成立。

结论：**没有证据表明 OpenAI 在 API 层偷偷用 DOM/a11y**；它是真·纯像素。反而是 Anthropic 现在同时提供两套：`computer_toolset`（截图 + 坐标）和 `browser_use_tool`（"member tools read and act on the page itself"，页面级寻址）【官方 computer use 页开头对比段】。**做 Web 任务时，Anthropic 官方推荐的已经是页面级那套，不是纯视觉那套。**

### 2.5 分辨率与坐标

- 声明式：`display_width` / `display_height` 告诉模型环境尺寸，坐标以该空间为准；实现侧只做 **clamp**，不做缩放【官方】。
- 截图要求：Azure 示例用 `page.screenshot(full_page=False)`——**只截视口，不含地址栏/浏览器 chrome**，并明确"model needs a way to capture screenshots … we're limiting the view to just the content in the browser window"。【官方】
- 频率：示例里**强制每次迭代截一张**（"we will force a screenshot to be taken for each iteration"），不依赖模型主动请求。【官方】
- 图像 token 规则：**OpenAI 未公布** computer use 截图的 token 计费细则【未验证】。

---

## 3. 关键对照

### 3.1 动作空间

| 维度 | Anthropic（20260801） | OpenAI（computer） |
|---|---|---|
| 粒度 | **更细**：17 个成员 | 较粗：9 种 action |
| 独有 | `zoom`（区域全分辨率）、`cursor_position`、`hold_key`、`left_mouse_down/up`、`triple_click`、修饰键 `text` 参数 | `click(button=back/forward/wheel)`（浏览器语义） |
| 批量 | 一次响应多个成员，**顺序执行、首错即停** | `actions[]` 数组，同样顺序执行 |
| 参数风格 | 独立成员 + `coordinate/text` | 单一 `action.type` 分派 |

Anthropic 更细，代价是模型要选 17 个工具（工具定义 token 更多）；OpenAI 更粗，把浏览器导航压进 `button` 枚举，表达式干净但只能覆盖浏览器。

### 3.2 坐标与缩放

| | Anthropic | OpenAI |
|---|---|---|
| 坐标系 | **你回传的截图像素**（新版已删 `display_*`） | 声明的 `display_width/height` |
| 缩放责任 | **调用方强制**：缩了图就必须把坐标乘回去；`tool_result` 图超限直接报错 | 调用方自行保证截图与声明尺寸一致；越界 clamp |
| 官方缩放帮助 | 参考实现给了 XGA/WXGA/FWXGA + `scale_coordinates()` | 无（只有 clamp） |
| 越界处理 | 抛错，喂回模型纠正 | 静默 clamp |

**Anthropic 的语义更适合"未来扩到桌面"**：坐标只跟图走，桌面/浏览器/远程 VNC 全都一样；OpenAI 的 `display_*` 需要你维护"声明尺寸 vs 真实尺寸"的一致性，多一层可能出错的映射。

### 3.3 纯像素 vs 文本信号

| | Anthropic computer | Anthropic browser use | OpenAI computer |
|---|---|---|---|
| 感知 | 截图 | 页面结构（页面级寻址） | 截图 |
| 有没有 DOM 通道 | 无 | 有（工具内部） | **无**（schema 就没有字段） |
| 官方定位 | 桌面/跨应用 | **Web 任务更合适** | 通用 GUI |

结论：**"纯视觉"不是唯一选项，也不是 Web 场景的默认最优**。Anthropic 自己在文档里把 Web 任务引向页面级工具；OpenAI 只有纯像素这一条路。

### 3.4 延迟与成本结构

| | Anthropic | OpenAI |
|---|---|---|
| 一次动作 = 一次模型调用？ | 是（除非 batch 一次返回多个动作） | 是（batch 的 `actions[]` 在同一轮返回） |
| 谁触发截图 | 模型请求（`screenshot`/`zoom`），或 batch 末尾 | **调用方每轮强制**（官方示例） |
| 每步图片 token | 1280×800 = 1,334（公式）【计算】；官方表：1920×1080 标准档 1,560 | 未公布【未验证】 |
| 历史膨胀 | 每轮重发历史截图，成本随步数近似二次增长 | 可用 `previous_response_id` 托管，但 token 账单仍包含历史截图 |
| 省成本手段 | `zoom` 代替整屏、降低截图尺寸、`thinking.effort` 调档 | 降低 `detail`、减少 batch 轮数 |

Anthropic 官方对 effort 的建议（【官方】Combining with thinking 节）：Opus 4.7 默认 `high`；Sonnet 4.6 / Opus 4.6 默认 `medium`，避免 `max`——"adds token cost without improving accuracy on UI tasks"。

### 3.5 长任务（几十步）怎么撑

| | Anthropic | OpenAI |
|---|---|---|
| 规划层 | **没有独立 planner**；靠模型自身 + 可选 thinking | **没有独立 planner**；靠 reasoning items + `summary` |
| 子目标 | 无 API 结构；文档建议"任务拆小、每步给明确指令" | 无 API 结构 |
| 防打转 | 应用层 `max_iterations`（示例 10）；batch 必须在下一个请求前全部答复 | 应用侧自行控制；官方示例也是固定循环 |
| 可观测性 | 文本 + 截图，动作本身可读 | `reasoning.summary` + `pending_safety_checks` |

**两家都没有"子目标/计划栈"这种工程结构**。所谓"分解成多步计划"是模型的 CoT 行为（OpenAI 博客原文 "break tasks into multi-step plans"），不是你可编程的接口。几十步任务要靠**你自己**加：步数上限、重复动作检测、失败回退。

---

## 4. 落到我们这边

### 4.1 现状（已核对的硬约束）

| 事实 | 证据 |
|---|---|
| 模型 `GLM-5.3-Flash`（经本地网关，该网关已于 2026-11-17 移除；现为直连 `deepseek-flash`） | `.env:3-5`【项目代码】 |
| 视觉开关为真，且实跑可读图 | `config.py:61`；`.env:7`【项目代码】 |
| **tool 角色不能带图**，图片只允许出现在 user 消息 | `agents/vision.py:7-12`【项目代码】 |
| `Read` 只回文本引用（`[图片] <路径> \| <mime> \| <n> 字节`），请求前还原成独立 user 消息 | `tools/fs.py:256-282`、`agents/vision.py:134-191`【项目代码】 |
| 每次请求最多注入 **6 张**图（按路径去重、从末尾往前） | `agents/vision.py:131`【项目代码】 |
| 单边 8192px 硬限；服务端把大图缩到 ~1300px 等效，单图 token 上限 1024 | `agents/vision.py:10-12`【项目代码】 |
| 独立内核 Chrome for Testing **158.0.8127.18**，不碰系统 Chrome | `cua-token-bench/out/result.json:5-6`【实测】 |
| `task_kind="cua"` 仍是 `implemented=False` | `agents/task_kinds.py:126-131`【项目代码】 |
| 预算 soft 256k / hard 320k；recursion limit 300 | `config.py:85-86`、`config.py:100-105`【项目代码】 |

### 4.2 纯视觉路线的具体设计

#### 截图从哪来

- 进程内 Playwright 驱动独立 Chromium（158 那份），`viewport = 1280×800`、`device_scale_factor = 1`、`--force-device-scale-factor=1`。
- `page.screenshot(full_page=False)` → **只截视口**，不含浏览器 chrome。**不要用 full_page**：实测整页 Wikipedia 是 `1280×48985`，直接触发 8192 单边限制（`result3.json:6-8`）。
- 落盘到 `workspace_root` 下的 `artifacts/shots/step-000N.png`，**不把 base64 写进 state**。

#### 怎么回灌

这是与我们架构最相关的一条：**Anthropic 那套"screenshot 作为 `tool_result` 的 image 块"在我们这里做不到**——tool 角色带图会 400（`agents/vision.py:7-12`）。

所以在我们的实现里：

1. 截图工具**只返回引用文本**，且必须调用 `agents/vision.py:54-56` 的 `format_image_reference()`——它是"生成与解析共用格式"的唯一入口（`tools/fs.py:32` 也是 import 它）。自己拼字符串大概率对不上正则，图就白截了。
2. `inject_image_blocks` 在下一次模型调用前把引用还原成**独立 user 消息**里的 `image_url`。
3. 模型看到的顺序是：`[上一步动作的 tool_result 文本] → [user: 这张图] → [本轮指令]`。官方那条"**指令文本放在图之前**能提升点击精度"就不成立了——图在指令前。**这是我们必须实测确认的偏差**（见 §4.4 的 A/B 项）。

#### 坐标怎么缩放

**结论：我们这条路线根本不需要缩放，只需要锁死。**

- 模型看到的是 1280×800 的视口截图；Playwright 的 `mouse.click(x, y)` 用的也是**视口 CSS 像素**；`device_scale_factor=1` 时二者相等。**屏幕是 2560×1440 也不影响**，因为我们从不截整屏。
- 真正要防的是三件事：
  1. `devicePixelRatio ≠ 1`（系统 150% 缩放）→ 启动参数强制为 1，并在截图后断言 `page.evaluate("window.devicePixelRatio") === 1`。
  2. **页面自己缩放**（`Ctrl+±`、响应式）→ 坐标空间不变，不用管；但布局变了会导致点空，属于命中率问题。
  3. **截图与坐标不同步**（滚动后点位偏移）→ 动作后必须重截，不允许复用上一帧。

如果将来要支持"整屏截图"（桌面迁移场景），再套 Anthropic 的 `scale_coordinates` 模式：缩放系数一处定义、双向使用、越界抛错。

#### 动作原语暴露成几个工具

**6 个**（不要 17 个，也不要 1 个万能工具）：

| 工具 | 参数 | 说明 |
|---|---|---|
| `browser_screenshot` | `{}` | 返回引用文本；同一轮最多 1 次 |
| `browser_click` | `x`, `y`, `button="left"`（left/right/middle）, `count=1`（1/2/3）, `modifiers?`（如 `["shift"]`） | 合并 left/right/middle/double/triple，省工具定义与歧义 |
| `browser_type` | `text` | 键入字面文本 |
| `browser_key` | `keys`（如 `"Enter"`、`"Ctrl+S"`）, `repeat=1` | 按键/组合键 |
| `browser_scroll` | `x`, `y`, `delta_y`, `delta_x=0` | 鼠标移到点再滚 |
| `browser_drag` | `from_x`, `from_y`, `to_x`, `to_y` | |

可选第 7 个：`browser_wait`（`ms` ≤5000）。**不建议**暴露 `cursor_position`——模型看不到光标，信息量低。

理由：Anthropic 17 个成员里有 4 个是 click 变体（用 `button`/`count` 可合并）、2 个是拖拽底层原语（我们不需要给模型 `mouse_down/up`）、`zoom` 在我们的 1280×800 下可以先用"重新截一张局部图"替代（见下）。工具定义越少，system prompt 越短，模型选错工具的概率越低。

**`zoom` 的替代**：截图工具加一个可选 `clip={x,y,w,h}` 参数，实现"区域截图"，语义等价于 Anthropic 的 `zoom`。但**模型看到局部图后仍要按全图坐标点击**——这一点必须在工具描述里写死，否则一定点错（Anthropic 官方专门强调过这条）。

#### 一步一拍，还是允许连发

**首版：一步一拍。** 理由是我们的约束，不是抄作业：

- 每个动作后都需要一张**新的 user 消息图**；连发 N 个动作会在一轮里攒 N 张图，而注入上限是 6 张（`vision.py:131`），第 7 张开始被丢——**连发反而会看到过期画面**。
- 一步一拍才能把"这个动作有没有生效"和"是哪一步失败的"对上，恢复逻辑简单。

**v2 再考虑连发**（≤3 个动作），条件：模型在每个序列末尾自己请求截图（对应 Anthropic 的 batch 约定），且执行器按序执行、首错即停、剩余回 `Not executed`。

#### 失败怎么恢复

| 失败 | 检测 | 恢复动作 |
|---|---|---|
| 坐标越界 | `0 ≤ x < 1280 && 0 ≤ y < 800` | 直接返回错误文本（不 clamp，让模型自己修正） |
| 点了没反应 | 动作前后截图 SHA256 相同 | tool_result 附一句"页面无明显变化，考虑 scroll / wait / 重新截图" |
| 原地打转 | 相同 `(x,y)` 连续 3 次 且 页面 hash 不变 | 拒绝执行，强制先 `screenshot`；累计 2 次则终止并上报 |
| 页面跳转/弹窗 | `page.on("framenavigated")`、`dialog` 事件 | 自动补一张截图，附在 tool_result 文本里说明"页面已跳转" |
| 步数失控 | 单任务动作数 > 25 | 强制收尾（理由：成本与延迟，不是 recursion 额度——额度是 300，`config.py:85-86`） |
| 渲染未完成 | 截图前 `wait_for_load_state("domcontentloaded")` + 300ms 稳定延迟 | 参考实现用的是固定 2s 延迟（`_screenshot_delay = 2.0`）【官方代码】 |

### 4.3 纯视觉的真实代价（不写轻）

**只有一条是"便宜"的：单张图的 token 固定且低。**

【实测】`cua-token-bench/out/result.json`（模型 `GLM-5.3-Flash`，当时经本地网关，网关已于 2026-11-17 移除，Chromium 158，viewport 1280×800）：

| 页面 | 截图 token | a11y 快照 token | 仅可交互行 |
|---|---|---|---|
| Wikipedia（信息密集） | **643** | **131,828** | 32,330 |
| httpbin 表单 | **643** | 329 | 160 |
| TodoMVC | **643** | 162 | 58 |
| Excalidraw（画布） | **643** | 626 | 343 |
| 640×400 截图（分辨率探针） | **201** | — | — |

- 图片 token **与页面内容无关**，只跟分辨率有关：1280×800 = 643，640×400 = 201；PNG/JPEG(q80) 同价。
- 长文页：图比整页快照便宜 **约 205 倍**（643 vs 131,828）。
- 表单/列表页：**ref 更便宜**（329 / 162 vs 643），图贵 2–4 倍。

**代价一：整页不可用。** Wikipedia 整页 `1280×48985`，超过 8192 单边限制被拒；缩到 2048 长边后是 `54×2048`（323 token），**字全糊了，等于没有信息**（`result3.json:4-10`）。纯视觉天然只能看视口，长页面必须"滚动 + 多次截"，**每次滚动都是一次模型调用**。

**代价二：累计成本不是 643，是 643 × 步数（带 6 张上限）。**
单请求最多 6 张图（`vision.py:131`）→ 单请求图片 token 上限 ≈ **3.9k**（6×643）。一个 20 步任务：
- 最少 20 次模型调用；
- 若每次都带满 6 张图，累计输入图片 token ≈ 20 × 3.9k ≈ **78k**；
- 加上文本（系统提示 + 历史动作）与输出，整任务 100k+ 量级【计算】。
- **同一张历史截图是否被网关缓存、是否重复计费，未验证**。若不缓存，这是纯线性叠加的成本。

**代价三：坐标误差是"静默失败"。** ref 点错会明确报 `stale ref` / `element not found`；坐标点错**什么都不报**，页面只是没变化。所有恢复逻辑都得靠"截图哈希对比"这种间接信号。

**代价四：可解释性。** 失败复盘时你只有一堆图和坐标，没有"它以为它点的是什么"。

### 4.4 精度：这是唯一的真问题

**未验证项：`GLM-5.3-Flash` 在 1280×800 截图上的点击命中率，本文没有测。**

已知的有利/不利条件：

- 有利：服务端把大图缩到 ~1300px 等效，而 1280×800 **本来就在这个范围内**（`agents/vision.py:10-12`），所以我们的截图**没有二次降采样损失**。
- 不利：小控件（<24px）、密集表格、图标按钮（无文字）在 1280×800 下每控件约 20–40px，模型要给到 ±10px 以内才稳。
- 未知：我们这条链路的**图在指令之后**（§4.2），而 Anthropic 官方明确说指令在图前更准——**这个顺序差异的影响没有量化**。

**所以第一步就是测，判据先定死：**

| 命中率 | 结论 |
|---|---|
| ≥90%（30 个目标错 ≤3） | 纯视觉做主路径，ref 只做兜底 |
| 70–90% | 纯视觉 + **吸附**（下面这套），可上线 |
| <70% | 纯视觉不做主路径，改"ref 为主 + 截图兜底" |

**吸附（hybrid-lite，几乎零成本，建议无论如何都做）：**

```
模型给 (x, y)
  → page.evaluate(document.elementFromPoint(x,y))
  → 若命中可交互元素（a/button/input/select/textarea/[role]/[onclick]）
        → 取该元素 boundingBox 中心点击
        → 同时记录 ref（供回放与日志）
  → 否则按原坐标点击
```

这不是"偷偷用 DOM 做感知"——**感知仍然是纯像素**（模型自己看图给坐标），DOM 只用于**执行时的容错**。好处：抵消 ±10px 误差、失败日志里能写出"它实际点到了 `<button>登录</button>`"、为将来切 ref 路线留下数据结构。代价：几十行代码 + 一次 `evaluate`。

**如果连吸附都不够**（命中率 <70%），再退到"视觉定位 + ref 校验"的完整混合：截图让模型说"我要点右上角搜索框"，DOM 里找候选元素画框回给模型选，选中后按 ref 执行。这条路 Anthropic 的 `browser_use_tool` 已经证明可行，但**要多写一套定位与框选逻辑**，不建议作为 v1。

---

## 5. 最终建议

**一句话：纯视觉值得做，但它是"用精度换迁移性"的交易——先花半天测命中率，再决定是单腿走还是加吸附。**

迁移性这条账是成立的：Anthropic 新版把坐标系改成"截图像素空间"、删掉 `display_*`，本质就是把"浏览器 / 桌面 / 远程 VNC"统一到同一套 `截图 → 坐标动作` 上；我们的 6 个工具原语也能原样复用到桌面（`browser_*` 改成 `computer_*`，实现从 Playwright 换成 pyautogui/win32，模型侧零改动）。这是 ref 路线给不了的。

但要说清三件事：

1. **Web 场景我们是在逆着两家的官方建议走**：Anthropic 明确把 Web 任务引向页面级工具，OpenAI 是纯像素但只在 Responses API 可用（我们接不了）。选纯视觉的理由是**迁移性**，不是"官方推荐"。
2. **代价不在单张图（643 token 很便宜），在步数**：滚动分屏、每步重截、6 张上限、重复计费未验证。
3. **成败在坐标精度**，而这一点现在没有数据。**别在没测之前把它写成"可行"**。

**落地顺序（再强调一次）：**

1. 跑 grounding 基准 → 拿到命中率数字；
2. 按数字选"纯视觉 / 纯视觉 + 吸附 / 混合"；
3. 只做浏览器视口内的事，25 步上限，一步一拍；
4. 桌面迁移留到第二步之后（换执行层，不换表示层）。

---

## 6. 证据清单

**一手文档**
- Anthropic Computer use tool：`https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool`
  - 已读：Security / Quick start / How computer use works / Batch actions / Prompting / System prompts / Available actions（17 成员表）/ Tool parameters / 坐标规则 / Combining with thinking
  - **未读到**：`Understand computer use limitations`、`Size screenshots to fit image limits` 正文（页面抓取截断）
- Anthropic 旧版（`computer_20250124`，含 ≤1280×800 建议）：`https://docs.claude.com/zh-CN/docs/agents-and-tools/tool-use/computer-use-tool`
- Anthropic Vision（token 公式与分辨率档位）：`https://platform.claude.com/docs/en/build-with-claude/vision.md`（`Resolution and token cost` 节）
- Anthropic 参考实现（缩放代码）：`https://raw.githubusercontent.com/anthropics/anthropic-quickstarts/main/computer-use-demo/computer_use_demo/tools/computer.py`（`computer.py:37-43`、`233-256`；仓库现已改名 `claude-quickstarts`）
- OpenAI Computer use 指南：`https://platform.openai.com/docs/guides/tools-computer-use`（模型 `computer-use-preview`、工具 `computer_use_preview`、5 步循环、reasoning items、safety checks、仅 Responses API）
- OpenAI CUA 产品公告：`https://openai.com/index/computer-using-agent/`（纯像素表述、benchmark 数字、Operator）
- Microsoft Azure OpenAI Computer Use（新一代 `{"type":"computer"}` 完整 handler，`actions[]`、`computer_screenshot`、`detail:"original"`）：`https://learn.microsoft.com/en-us/azure/foundry-classic/openai/how-to/computer-use`

**本机实测**
- `cua-token-bench/out/result.json`：四类页面 a11y vs 截图 token、640×400 探针、浏览器版本
- `cua-token-bench/out/result3.json`：整页截图 1280×48985 → 2048 长边后 54×2048
- 产物：`out/wiki-aria.txt`、`form-aria.txt`、`todomvc-aria.txt`、`excalidraw-aria.txt`、各 `*.png`

**本仓库代码**
- `config.py:53,55,61,85-86,100-105`
- `.env:3-7`
- `agents/vision.py:7-12,47-51,54-56,131,134-191`
- `tools/fs.py:32,47,256-282`
- `agents/task_kinds.py:126-131`

**明确未验证**
- `GLM-5.3-Flash` 在 1280×800 截图上的坐标命中率
- 网关是否对重复图片做 prompt cache（影响累计成本）
- Anthropic 当前版 limitations 章节正文
- Anthropic 多显示器方案（新 toolset 已删除 `display_number`）
- OpenAI computer use 截图的 token 计费规则
- OpenAI Operator 产品当前形态（是否仍独立）
- 检索摘要中的"桌面 1024×768 / Web 1280×800"推荐（未逐字核对原文）

---

## 7. 补充调研：两家的页面级 browser use（2026-11-13）

### 7.1 Anthropic：`browser_toolset_20260801`

**形态**【官方】`https://platform.claude.com/docs/en/agents-and-tools/tool-use/browser-use-tool`

- `tools` 里一个 `{"type": "browser_toolset_20260801"}` 条目（**不带 `name`**），默认展开 **27 个成员工具**，另可在启用后增加 **4 个可选成员**：`javascript_exec`、`file_upload`、`read_console`、`read_network`。
- 每个成员调用带 `"toolset_name": "browser"`，`input` 只有该成员自己的参数，**没有 `action` 字段**（与 computer toolset 同构）。
- 执行器是**你自己的浏览器**："您的应用程序针对其自身的浏览器自动化运行每一次调用；没有任何内容在 Anthropic 一侧运行。"

**和 `computer_toolset_20260801` 的关系**：**并列的两套 client toolset，不是父子/成员关系**。官方选择建议（原文）：

| 任务 | 官方推荐 |
|---|---|
| 任务始终在网页内、或页面由 JS 构建、需要对页面"执行操作" | **browser use**（能读结构、能按 ref 操作、能直接设表单值、能跨标签页，且不用跑桌面） |
| 需要整个桌面 | computer use（"works through screenshots and coordinates alone"） |
| 只需要读你指得定的页面 / 搜资料 | `web_fetch` / `web_search`（服务器工具，没有浏览器要操作） |

**能否混用**：一个请求里同时声明两套 toolset 的写法，本文**没有找到官方明文**，标【未验证】。但注意下面这条——**browser toolset 本身已经能按坐标点击**（见"定位"），所以"纯视觉"在它内部不是一个需要另外混入的模式。

**表示层：不是纯结构，是结构 + 像素的混合**【官方】

原文（中文版）："它既通过页面的结构（**无障碍树**、元素、表单和标签页）也通过**像素**（屏幕截图和视口坐标）来处理页面，而计算机使用工具则仅通过屏幕截图和坐标来处理整个桌面。"

各成员返回什么：

| 成员（示例中出现的） | 返回 |
|---|---|
| `navigate` | 文本 + `browser_state` 块（`tabs[]`：`tab_id`/`title`/`url`/`active`） |
| `read_page` | **文本**，每行一个元素，带引用：`link "Documentation" [ref_1]` |
| `find` | 同上，产出 ref |
| `get_page_text` | 正文文本（无 ref） |
| `screenshot` / `zoom` | **image 块**（base64 PNG） |
| `new_tab` / `switch_tab` / `close_tab` / `list_tabs` | **恰好一个** `browser_state` 块，无文本无图 |
| 其它动作（`left_click`、`type`、`key`、`scroll_to`、`form_input`、`file_upload`…） | 文本确认，可附带 `browser_state` |

表里只是官方示例/表格中出现过的成员；**完整 27 项清单所在的 Member tools 表格被页面抓取截断，未逐字获取**。已确认存在的名字：`navigate`、`read_page`、`find`、`get_page_text`、`screenshot`、`zoom`、`left_click`、`right_click`、`middle_click`、`double_click`、`triple_click`、`hover`、`left_click_drag`、`left_mouse_down`、`left_mouse_up`、`mouse_move`、`scroll`、`scroll_to`、`type`、`key`、`form_input`、`file_upload`、`new_tab`、`switch_tab`、`close_tab`、`list_tabs`。

**定位怎么落到元素上**【官方】

- `target` 有两种形状：`{"type":"coordinate","x":…,"y":…}`（视口像素）或 `{"type":"ref","ref":"ref_2"}`。
- **ref 由执行器生成并维护**："您的执行器分配这些引用，保存从每个引用到底层节点（**无障碍节点 ID、存储的选择器或等效物**）的映射，并在引用传回时对该节点执行操作。"
- ref **作用域限于生成它的标签页**，在该标签页导航或 **DOM 实质性变化**前有效；"API 无法检测过期或未知的引用"——执行器必须返回错误结果，官方给的文案是：
  `Error: ref_3 is stale or not found on the current page. Re-read the page to get fresh references.`
  模型随后会重读页面。**导航前不要对已分发的 ref 重新编号**，否则会静默作废模型手里的引用。

**坐标约定**（和 computer toolset 的差异）【官方】

- 坐标是**视口像素**：全视口 `screenshot` 的像素空间，原点在渲染页左上，**不含桌面/窗口边框**。
- **工具集不声明任何显示尺寸**，"Claude 从您返回的屏幕截图推断视口大小，因此请保持它们尺寸一致"。
- `zoom` 不改变坐标系：`region` 和 zoom 后发出的坐标仍是全视口像素。
- **toolset 图像不会被 API 自动缩小**：超限直接拒绝；且一次请求超过 **20 张图**时适用更严的单图限制。缩了图要按倒数把坐标乘回去。

**大页面怎么收敛**【官方】：`read_page` 支持 `filter: "interactive"` 或传一个**容器 ref** 读子树。官方原话："对典型页面的树读取通常比屏幕截图消耗更少的输入令牌，同时为 Claude 提供可以立即据以执行操作的引用。"

**元素数量 / 深度上限**：**未获取到**（Member tools 与 read_page 参数节被截断），标【未验证】。可确认的只有"用 filter 或容器 ref 收缩"这一种官方手段。

**iframe / shadow DOM / 虚拟列表 / canvas**【官方】——这段直接回答"结构路线的软肋"：

> "对于树未描述的内容回退到坐标。 **Canvas 渲染的界面、嵌入式视频或远程桌面表面、高度虚拟化的列表以及跨源 iframe 内的元素**通常没有有用的节点，因此 Claude 依靠 `screenshot` 和 `zoom` 工作并按坐标点击；**您的执行器负责解析坐标落在哪个框架中**。"

- 跨源 iframe：官方承认没有节点，退坐标；frame 解析责任在执行器。
- shadow DOM：**全文未提及**，标【未验证】。
- 虚拟列表：官方点名"highly virtualized lists"没有有用节点 → 退坐标。

**官方给的两条安全约束**（与我们设计相关）：页面读取结果要**从渲染内容（无障碍树或可见文本）构建，不要用原始 DOM 源码**，避免隐藏文本进 prompt；`javascript_exec` / `file_upload` 默认关闭。

**一句话**：Anthropic 的 browser use 不是"纯结构路线"，而是**"ref 优先、坐标兜底、截图可读"的官方混合体**——它自己就在做我们讨论的混合方案。

### 7.2 OpenAI：API 侧没有页面级工具，产品侧有

**结论：API 侧没有独立的 browser tool。** 依据【官方】：

1. `https://platform.openai.com/docs/guides/tools` 的内置工具列表只有：Web search、File search、Tool search、Function calling、Remote MCP，以及导航里的 **Computer use**；**没有 browser / DOM / page 类工具**。
2. `https://platform.openai.com/docs/guides/tools-computer-use` 里，模型与环境的全部通道就是截图：输入是 `computer_call_output` 里的 `computer_screenshot`，输出是 `computer_call.action`（坐标/按键/滚动）。
3. Azure 官方 handler 的动作集合（`click`/`double_click`/`drag`/`move`/`scroll`/`keypress`/`type`/`wait`/`screenshot`）**没有一条接收或返回页面结构**。

`web_search` 不等于 browser tool：它是检索（返回结果/引用），不驱动页面、不产生元素引用。

**产品侧确实有混合，但不在 API 里暴露**【官方】
`https://openai.com/index/introducing-chatgpt-agent/`（2025-07-17）原文列了四个工具：

> "a **visual browser** that interacts with the web through a graphical-user interface, a **text-based browser** for simpler reasoning-based web queries, a terminal, and direct API access."

以及："the model can choose to open a page using the **text browser or visual browser**"；"gather information about your calendar through an API, efficiently reason over large amounts of text using the **text-based browser**, while also having the ability to interact **visually** with websites designed primarily for humans."

所以：**OpenAI 的"页面级"能力存在于产品内部（text browser），API 侧只开放纯像素的 computer use。** 这也是为什么"OpenAI 也有页面级方案"这个说法**对第三方不成立**——你拿不到那个 text browser。

### 7.3 三条路线对照（核心）

| 维度 | ① 纯视觉（computer use） | ② 页面级寻址（browser use） | ③ 我们的 ref 快照 |
|---|---|---|---|
| 表示层 | 截图（像素） | a11y 树文本 + ref + 截图 + 标签页状态（**混合**） | a11y/DOM 精简文本 + ref |
| 谁负责定位 | **模型**（给坐标） | **执行器**（ref→节点映射由你维护），模型只给 ref | 执行器（ref→locator） |
| 单步 token | **643**（实测，1280×800，我们的模型）；与页面内容无关 | 无公开数字【未验证】；官方定性："典型页面的树读取通常比截图消耗更少输入令牌" | **162 / 329 / 626**（TodoMVC / 表单 / 画布，实测）；长文整页 **131,828**（仅可交互 32,330） |
| 动态页面（JS/懒加载） | 好：看到什么就是什么 | 好：每步重读即可；但 ref 会因 DOM 变化失效 | 中：需重新快照，ref 会 stale |
| iframe | 中：弹出/内嵌内容一律当像素，**不用解析 frame** | 弱：**跨源 iframe 官方承认无节点，退坐标**；frame 解析责任在执行器 | 弱：跨源 iframe 通常拿不到 |
| canvas | **好（唯一能读的）** | 弱：官方点名 canvas 无节点 → 退坐标 | 弱：无节点 |
| shadow DOM | 好（像素无关） | **未知**（官方未提及）【未验证】 | 弱（需自己穿透） |
| 精度 | 取决于模型 grounding，**未测**【未验证】 | 高：ref 命中即精确 | 高：同 ref |
| 可复现性 | 低：同一屏两次可能给不同坐标 | 中：ref 每次重分配，但结构稳定 | 高：选择器/ref 可回放 |
| 跨站迁移成本 | **低**：一套提示 + 一套坐标规则 | 中：每站自定义组件都要调；官方靠 filter/容器 ref 收敛 | 中–高：快照裁剪规则要按站点调 |
| **扩到整个桌面** | **直接扩**：换执行层（Playwright → pyautogui/win32），表示层零改动 | **不能扩**：工具集语义就是"网页内"，桌面无 a11y 树可读 | **不能扩**：同上 |
| 官方定位 | 整个桌面 | 网页内 | — |

**关于"扩到桌面"这条，给一个诚实评估**（这部分是判断，不是事实，标【判断】）：

- 纯视觉的优势是**真的**：表示层（截图 + 坐标 + 动作原语）在浏览器和桌面完全同构，`browser_*` 改 `computer_*` 即可，模型侧 prompt 几乎不动。
- 但它**没有"省掉一半工作"那么多**。桌面真正难的不是表示层，是：窗口焦点、DPI/多显示器、坐标到屏幕的映射、权限（macOS 辅助功能授权）、无头环境不可用、动作后全局状态不可控。这些在浏览器里被 Playwright 屏蔽掉了，到桌面要重新做一遍。
- 反过来，页面级路线的迁移成本不是"改一改"，而是**归零**：浏览器之外没有 a11y 树这个概念，整套工具集作废。
- 所以选纯视觉的合理性成立，但收益应表述为"**表示层复用，执行层重写**"，而不是"一套循环通吃"。

### 7.4 对我们的判断

在"纯视觉主路径 + 未来扩桌面"的前提下，页面级寻址**仍有价值，但只在两层**：

1. **执行容错（吸附）**：坐标 → `document.elementFromPoint(x,y)` → 命中可交互元素就点它的包围盒中心。感知仍是纯像素，DOM 只用来抵消 ±10px 误差。这就是 Anthropic 自己在 browser toolset 里做的事（ref 与坐标双 target），只不过我们只用后一半。
2. **定位校验（诊断）**：动作后记录"坐标实际命中了哪个元素"，失败日志里能写出 `expected: 搜索按钮 / actual: <div class="banner">`。这解决纯视觉"失败无声"的问题，成本只有一次 `evaluate`。

**不建议**把完整 ref 快照保留成"另一条兜底主线"：两套表示层并行维护，迁移到桌面时 ref 那套必然是死代码，且会诱导模型在两套动作间摇摆。

**一句话建议：纯视觉主路径 + DOM 吸附/校验兜底；browser use 只借它的两个设计——`target` 的双形状（coordinate 与 ref 同构）和 stale ref 的错误文案规范——不引入整套工具集。**

**分流建议（如果将来 Web 任务量足够大）**：按 Anthropic 的官方分流做两个 `task_kind`（`cua_browser` 走 browser use、`cua_desktop` 走纯视觉），而不是在一个循环里塞两种表示。这两条路的失败模式、预算模型、测试方法都不一样，混在一起会同时失去两者的优点。

> **2026-11-14 定稿修正**：上面这条"两个 task_kind"的分流建议**不作数**，被 **§8.7** 取代为单一主线 `cua`（浏览器与桌面同一套原语）。原文保留只为留痕。

**本节新增证据**

- Anthropic Browser use tool：`https://platform.claude.com/docs/en/agents-and-tools/tool-use/browser-use-tool`（含 Quick start、Batch actions、Targets and coordinates、Element references、Fallback 段落、Security considerations）；中文版同路径 `/zh-CN/`
- OpenAI Using tools（内置工具清单，无 browser tool）：`https://platform.openai.com/docs/guides/tools`
- OpenAI ChatGPT agent（visual browser + text-based browser）：`https://openai.com/index/introducing-chatgpt-agent/`
- OpenAI Computer use 指南（纯截图通道）：`https://platform.openai.com/docs/guides/tools-computer-use`

**本节新增未验证项**

- 一个请求里同时声明 `browser_toolset_20260801` 与 `computer_toolset_20260801` 是否合法
- browser toolset 的 27 个成员完整清单、`read_page` 的元素数量/深度上限（页面截断）
- shadow DOM 的处理方式（官方全文未提及）

---

## 8. 定稿：纯视觉 computer use（2026-11-14）

> 本节是选型结论，不是再调研。事实来源只有两类：**本机实测**（见 `docs/CUA_DESKTOP_FEASIBILITY.md`）与**官方文档**；没有数据的地方标【未验证】，不为了完整性编数。
>
> **2026-11-14 修订**：§8.2 末尾"每个 `tool_result` 后强制追加带图 user 消息"这条**已被证伪**（一轮多个 `tool_call` 时不成立），修正见 **§8.9**；该段的原文保留并标注，不静默改写。

### 8.0 定位与一句话

- **做什么**：纯视觉的 computer use agent，**不是只做 browser**。桌面与浏览器共用同一套"截图 + 坐标动作"原语，不为网页单独做一套表示层。
- **取景范围**：**只做单屏**，双屏场景不支持。主屏实测 3072×1920 @200%，管线缩到 2048 长边（BICUBIC → JPEG q75）。
- **一句话**：一个 task_kind（`cua`）+ 12 个原语工具 + 1 个批次入口 `act`（§8.9.4）+ 一个固定 1.5× 的坐标换算；**主路径唯一**，ref/DOM 只允许做离线校验，不做并行主线。

### 8.1 总体形态：cua worker

| 项 | 定稿 |
|---|---|
| 接入方式 | `create_session` 的 `task_kind` 新增 `"cua"`，与 `general` 并列 |
| 进程模型 | **与 general worker 一致**：每任务起新进程，任务结束销毁 |
| 状态持久化 | checkpointer 存 state，**不存图片**；图片以 `{path, sha256, width, height, origin, scale}` 引用 |
| 图片生命周期 | 落临时目录，会话结束删除；恢复会话时若引用失效就**重截一张**（历史截图对下一步决策没有价值） |
| 不做的 | 不新建"常驻桌面服务"、不复用 general 的浏览器上下文、不引入第二套会话类型 |

不存图片的理由是量级：2048×1280 JPEG q75 单张 **155,448 B**（实测），base64 后 207,264 字符；一个几十步的任务就是 MB 级 state，而历史图只在"回看"时有用，属于日志而非状态。

### 8.2 工具清单（12 个，按我们的原语能力裁剪）

原语能力来自实测（`CUA_DESKTOP_FEASIBILITY.md` §4/§5）：绝对移动误差 0–3 px、点击/双击/右键/滚轮/Unicode 键入全部命中、窗口可枚举（33 个窗口 / 15 个 PID）、VK 按键被输入法拦截。

| 工具 | 参数 | 返回 | 为什么需要 | 与 Anthropic 的对应 |
|---|---|---|---|---|
| `screenshot` | `{}`（不接受 monitor 参数） | `{image_ref, width:2048, height:1280, origin:[0,0], scale:1.5, mean_luma}` | 唯一观测通道 | 同 `screenshot` |
| `zoom` | `{x0,y0,x1,y1}`（全图像素空间） | `{image_ref, region, width, height, scale:1.0}` | 长文本页/小字：2048 宽下 1 个界面像素 ≈ 0.67 模型像素，8–10 px 的字被压到 5–7 px | 同 `zoom`（实现不同，见 §8.4） |
| `click` | `{x, y, button:"left"\|"right"\|"middle", count:1\|2\|3}` | `{ok, at:[x,y], button, count}` | 最常用动作 | **合并** `left_click`/`right_click`/`middle_click`/`double_click`/`triple_click`（5→1） |
| `mouse_move` | `{x, y}` | `{ok, at:[x,y]}` | hover 出菜单/提示，很多 UI 只有 hover 才显示 | 同 `mouse_move` |
| `drag` | `{from:[x,y], to:[x,y], button:"left", duration_ms:300}` | `{ok, from, to}` | 拖选、拖放、滑杆 | **合并** `left_click_drag` + `left_mouse_down` + `left_mouse_up`（3→1） |
| `type` | `{text}` | `{ok, chars}` | 文本输入；只走 `KEYEVENTF_UNICODE` | 同 `type` |
| `key` | `{keys:"enter" \| "ctrl+s"}` | `{ok}` 或 `{error:"ime_intercept"}` | 快捷键仍需要（保存、切换、确认） | 同 `key`（**本机受限**，见 §8.6） |
| `scroll` | `{x, y, direction:"up"\|"down"\|"left"\|"right", amount:int}` | `{ok, notches, at}` | 长页面/长列表 | 同 `scroll`（坐标必填，不给默认值） |
| `wait` | `{ms}`（运行时上限 5000） | `{ok}` | 等加载/等动画 | 同 `wait` |
| `list_windows` | `{}` | `[{hwnd, pid, proc, title, class, rect, focused}]` | 纯视觉在桌面上"我在哪、能操作谁"；也是安全白名单的输入 | **新增**（Anthropic 的沙箱只有一个容器，没有这个问题） |
| `focus_window` | `{hwnd}` | `{ok, hwnd, rect}` | 焦点错了后面全错；切窗口是最常见的前置动作 | **新增**（同上） |
| `session_state` | `{}` | `{locked, connect_state, input_desktop_ok}` | 锁屏/UAC 安全桌面时**拒绝动作**而不是盲点 | **新增**（同上） |

**砍掉的四个**（Anthropic 有，我们不做）：

- `cursor_position`：位置已经出现在 `click`/`mouse_move`/`drag` 的返回值里，单独查一次等于多一轮往返。
- `hold_key`：实测没有任何场景需要"按住 N 秒"，真正需要长按的是拖拽，已被 `drag.duration_ms` 覆盖。
- `triple_click`：并入 `click.count=3`，不单独占一个成员。
- `left_mouse_down` / `left_mouse_up`：并入 `drag`；保留它们只会诱导模型拼出不完整的拖拽序列。

**不新增的工具与理由**：

- `open_app`：启动程序走开始菜单的视觉操作即可；新增 launch 会带来一条绕过视觉的旁路，破坏"看到什么才能点什么"的一致性。
- `ocr` / `read_text`：一旦有了文本通道，模型就会优先读文本而不是看图，纯视觉假设失效；需要看字用 `zoom`。
- browser 专属工具（选择器/ref）：见 §8.7，不进入在线循环。

**"看结果确认"不是工具，是运行时规则**：~~每个动作的 `tool_result` 之后，运行时**强制追加一条 user 消息并附带最新截图**。~~ 网关对"tool 角色带图"返回 400（`agents/vision.py:6-9`），所以图片必须分开发 `user` 消息——这半句仍然成立；但"**每个** `tool_result` 之后"只在"**每轮一个 `tool_call`**"时成立：一轮返回多个 `tool_call` 时，assistant 的 `tool_calls` 后面必须紧跟对应的全部 `tool` 消息，中间插 `user` 会破坏邻接，图只能挂在**最后一条** `tool_result` 之后。

> **2026-11-14 修订**：本段原结论作废，保留原文以便追溯。正确定义、官方原文依据、三种调度方案的对比与取舍见 **§8.9**。

### 8.3 取景与坐标空间

**DPI 声明（硬要求）**：进程启动即 `SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2)`。

实测依据：不声明时，3072×1920 的屏只能截到 **1536×960（左上角 1:1 裁剪，丢 75%）**，且所有坐标 API 静默按 50% 折算，**不报任何错**；声明 system aware 时，多屏会把 100% 的副屏虚报成 2×。单屏下 system aware 其实也不会错，但统一用 V2——不给自己留"只在单屏成立"的隐性依赖。

**取景**：

| 项 | 值 | 依据 |
|---|---|---|
| 物理分辨率 | 3072×1920 | 实测 |
| 送模型的图 | **2048×1280** | `agents/vision.py:49` `_INJECT_MAX_EDGE=2048`；`:90-95` `resize()`（BICUBIC）；`:96-99` JPEG 默认质量 75 |
| 单张体积 | 155,448 B（base64 207,264 字符） | 实测 |
| 缩放系数 | **1.5（精确，3072/2048）** | 由长边上限决定，不是读取系统缩放比例算出来的 |
| 整屏取景 | **够用**（定稿） | 用户目视 2048×1280 成品图后认可 |
| 按窗口截图 | 降为**优化项**，不进第一版 | 未实现、未测【未验证】 |

**坐标空间**（只有一套）：

- 模型看到的是 **2048×1280** 这张图，原点 `(0,0)`；模型回传的坐标**就是这个像素空间**。单屏实测无负坐标，虚拟屏原点就是 `(0,0)`。
- 运行时换算：`physical = round(model * 1.5)`，反向 `model = round(physical / 1.5)`。
- 越界处理：超出 `[0,3071]×[0,1919]` 返回 `is_error`（让模型自己纠正），**不静默 clamp**；只有 ±2 px 的归一化取整误差允许夹紧（实测移动误差上界 3 px）。
- 每次 `tool_result` 都带 `{width, height, origin, scale}`，让模型知道自己在哪个空间——取代"用提示词叮嘱屏幕分辨率"这种不可靠做法。

### 8.4 与 Anthropic 对齐的三条关键做法

1. **截图坐标空间 = 模型回传的坐标空间**（已按 §8.3 落实）。
   官方原文："Every `coordinate` … is in the pixel space of the full-display screenshots you return"；"If you scale screenshots down before returning them, scale Claude's coordinates back up before applying them to the real display."
   出处：`https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool`（Available actions 节）。

2. **提供 zoom 作为长文本页的补救**，但实现简化：
   - Anthropic：截该区域**按全分辨率**取，再缩到常规截图尺寸，坐标仍按全图空间表达。
   - 我们：原生 1:1 裁剪直接返回（长边仍受 2048 限制），**坐标仍按 2048×1280 全图空间表达**，`zoom` 结果里明确带回 `scale:1.0` 和 `region`。
   - 理由：我们的缩放系数是固定的 1.5，局部再放大 1.5 倍等价于"1 模型像素 = 1 物理像素"，已经够读小字，不需要 Anthropic 那套"缩回常规尺寸再比对"的复杂度。

3. **指令文本必须放在图片之前**——**当前实现相反，标为待改（P1）**。
   官方要求："Providing the target description before the image is processed improves click accuracy."
   现状：我们的注入顺序是 `tool_result`（文本）→ 新 user 消息（**图片在前**，若有附加说明则在其后）。
   改法：需要附加说明时，把说明文本放在该 user 消息 content 的**第一段**，图片在后；若网关强制 image 必须首位，则退化为"先发一条纯文本 user 消息，再发一条只含图片的 user 消息"。**未改代码，仅登记。**

### 8.5 单屏简化后消失的问题（明确"不做"）

| 问题 | 状态 | 为什么现在不用管 | 如果将来捡回来要注意 |
|---|---|---|---|
| 负坐标 | **不做** | 单屏虚拟屏原点就是 (0,0)；负 y 只因为副屏放在主屏上方 | 一旦加屏，坐标范围必须改成虚拟屏 union，且 `SetCursorPos` 与窗口 API 的虚拟化行为不同（实测） |
| 混合缩放（200% + 100%） | **不做** | 只有一块屏，只有一个缩放比 | 必须"每屏一套坐标、每屏独立缩放"，不能用一个全局系数 |
| 虚拟屏黑边 | **不做** | 黑边是 system-aware 进程在多屏下的伪影（实测 61.3% 黑） | 若出现黑边，先检查 DPI 声明，不要加裁剪逻辑去"修" |
| `monitor` / `display_number` 参数 | **不做** | `screenshot` 只截主屏，不接参数 | 加参数意味着模型要理解"多显示器拓扑"，这是认知负担不是能力 |

写这段的目的：避免以后有人看到"多屏也能跑"就把这些又加回来——它们不是没做完，是**明确不做**。

### 8.6 未解决项

| # | 问题 | 现状 | 影响 | 下一步 |
|---|---|---|---|---|
| 1 | **中文输入法吞 VK 按键** | 实测：`SendInput(wVk=…)` 返回 1，控件收到 `keycode=229 (VK_PROCESSKEY)`；纯 scan code 同样被吃；把 IME 上下文从自建窗口摘掉仍无效（疑似 TSF 路径），**根因【未验证】** | `key` 工具在本机不可用（Enter/Tab/Ctrl+S 全部受影响）；`type` 不受影响（Unicode 路径实测精确） | `key` 前置检测 IME 打开状态，命中时报 `ime_intercept`；关键路径改用菜单点击或 Unicode。根因需单独排查 |
| 2 | **2048 宽下的可读性** | 用户**目视**看过成品图，认为清晰度可接受；这是**人眼判断，不是模型实测** | 小字（8–10 px）能否被模型读对，**没有任何数据**【未验证】 | 放进 P0 闸门，用真实任务测。**更正**：本文早前出现的"缩小 3.66×"是"虚拟屏缩到 819×800"的假设档，不是管线行为；管线真实缩放是 **1.5×**（3072→2048） |
| 3 | **坐标命中率** | **无数据**。本次只测了"我给的坐标能落在我自己的窗口"（误差 0–3 px），**没有测**"模型看图给的坐标能不能落在目标控件" | 这是纯视觉路线的唯一真问题（§4.4），命中率低则整条路线不成立 | **P0 闸门保留**：3 类界面 × 10 个目标 = 30 个点击，命中率 ≥90% 才继续 |
| 4 | **单张图的 token 成本** | 只有历史基准 1280×800 = 643 token（`cua-token-bench/out/result.json`）；**2048×1280 的 token 数【未验证】** | 直接决定几十步任务的总成本与可行性 | 与闸门 3 同批测，用网关真实返回的 `usage.prompt_tokens` |

### 8.7 与现有 browser 方案的关系

**结论：单一主线，不做并行。**

| 层 | 定稿 |
|---|---|
| 主路径 | `cua` worker 的纯视觉循环，**浏览器任务也走它**（地址栏、标签页、页面内容全部视觉操作） |
| ref / DOM 快照 | **不进入在线决策循环**。只保留两种离线用途：① 动作后用 `elementFromPoint` 做诊断（"实际命中了谁"）；② 基准测试里当 ground truth |
| 旧 browser worker | 不作为 CUA 的并列主线保留；已有能力如果依赖 DOM/选择器，按"执行层"而非"表示层"看待，不向模型暴露 |
| §7.4 的分流建议 | **被本节取代**（原文保留以便追溯）：不再设 `cua_browser` / `cua_desktop` 两个 task_kind |

**代价要承认**：在浏览器里，纯视觉比 DOM 寻址慢且更不稳（每步都要截图 + 模型 ground，DOM 一次选择器就够）。这是为"一套原语同时覆盖桌面和浏览器"付的钱。如果将来 Web 任务量大到不可接受，再重开"按任务类型分流"的决策——但那是**新决策**，不是现在预留双主线。

### 8.8 与 §4 / §5 的关系

- §4.2 的循环设计（单动作单拍、`button`+`count` 统一参数、坐标 clamp 策略）继续有效；本节把它收敛成 12 个工具的定稿，**没有推翻**。其中"单动作单拍"的粒度由 **§8.9.4** 收紧为"每轮一批、批末一张图"，单品动作仍是合法的最短批。
- §4.4 "精度是唯一的真问题"**不因为定稿而降级**，它现在是 §8.6 的第 3 项 P0 闸门。
- §5 的一句话建议**由 §8.0 取代**。
- 文档结构上，§1–§3 仍是选型依据（两家的动作空间/坐标/循环/成本），§7 仍是"为什么不走页面级"，§8 是结论。

### 8.9 批次、串行与截图节奏（2026-11-14 修订）

> 修订起因：三条批注指向同一处缺口——**我们的 tool 通道与 Anthropic 不同，"每步截图 + 严格串行"不能照搬进 langgraph**。本节只摆事实（官方原文 / 框架源码 / 本机探针）再给方案；既定口径（单屏、整屏取景够用、命中率仍是 P0 闸门）不动。

#### 8.9.1 Anthropic 的 batch 到底要求什么

三条硬要求，逐字引用【官方】Batch actions 节：

1. **不是每步都要图**：
   > "Only `screenshot` and `zoom` results need an image; for the other members, a short text acknowledgment such as `OK` is enough (`cursor_position` returns the coordinates as text)."
2. **但每个 `tool_use` 都必须有 `tool_result`**（文本即可）：
   > "Every `tool_use` block still needs a `tool_result`" / "A request that leaves any `tool_use` block in the batch unanswered is rejected with an `invalid_request_error`."
3. **按顺序执行、首错即停**：
   > "Run the blocks sequentially in the order they appear in `content`, and if one fails, don't run the rest." 后续动作回 `is_error: true` + 固定文案 `Not executed: an earlier computer action in this turn failed.`

所以用户直觉成立：**连发时不必每步都截图**；批次里 9 个动作可以只回 9 个 `"OK"` + 最后 1 张图。

之前被截断的那句，完整原文是：

> "Claude typically finishes a batch with `screenshot` so it can observe the outcome before determining what to do next. When a batch doesn't end with one, **your application can attach a screenshot as an extra `image` block on the last result in the batch** so that Claude always receives the current state of the screen, which **saves a round trip** compared with waiting for Claude to ask. You can also prompt Claude to end every batch with a screenshot."

即：**官方推荐把截图放在批次末尾**；模型没放时，**调用方自己在最后一条 `tool_result` 上挂一张**，省掉的正是"模型再发一次 `screenshot`"那一轮。

一条结构差异别忽略：Anthropic 的图是**塞在 `tool_result.content` 里的 image 块**（`tool_result` 本身在 user 消息内），根本没有"tool 消息之后再补一条 user"这回事。我们的网关不是这个结构，而这正是下面 8.9.3 的约束来源。

出处：`https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool`，How computer use works 第 3 步 / Batch actions 节。

#### 8.9.2 我们的调度器为什么会并发

源码事实（本机 `feishu` 环境实装版本：`langgraph 1.2.20` / `langchain 1.4.9` / `langchain-core 1.7.6`，与 `requirements.txt` 一致）：

| 事实 | 证据 |
|---|---|
| `create_agent` 只建**一个** ToolNode；该节点从最后一条 AIMessage 取出**全部** `tool_call` 一起执行 | `langchain/agents/factory.py:1140-1148`（只 `ToolNode(...)` 一次）；`langgraph/prebuilt/tool_node.py:807`（`tool_calls, input_type = self._parse_input(input)`） |
| 同步路径：线程池 `executor.map` 并发派发 | `langgraph/prebuilt/tool_node.py:829-832` |
| 池大小 = `config.get("max_concurrency")` | `langchain_core/runnables/config.py:672-675`（`ContextThreadPoolExecutor(max_workers=config.get("max_concurrency"))`） |
| 异步路径：`asyncio.gather`，**无任何并发上限** | `langgraph/prebuilt/tool_node.py:863-866` |
| 本仓库走**同步**路径 | `agents/base.py:184` `for chunk in graph.stream(...)` |

本机探针实测（3 个各 `sleep(0.3)` 的工具挂在一个 ToolNode 上，一条 AIMessage 带 3 个 `tool_call`）：

| 配置 | 总耗时 | 实际线程数 | 实际执行顺序 |
|---|---|---|---|
| 默认 | **0.323 s** | 3 | 三个工具在同一毫秒起跑 → **并发** |
| `{"max_concurrency": 1}` | **0.905 s** | 1 | a1→a2→a3，严格按 `tool_call` 顺序 → **串行** |

结论：**默认并发；`max_concurrency=1` 能在同步路径串行化并保持顺序，但有三个隐藏条件**——

1. 它是**整个 run 的全局上限**，会连带限制其它并行节点；
2. **异步路径不读它**：`_afunc` 是裸 `asyncio.gather`。哪天把 `graph.stream` 换成 `astream`（或引入异步工具），串行化会**静默消失**，且没有任何测试会报错。
3. "保持顺序"不是 ToolNode 的契约，只是 `ThreadPoolExecutor` 单 worker 的 FIFO 行为【实测，非文档保证】。

#### 8.9.3 硬约束：`tool` 消息顺序 + 网关 400

- 网关：`tool` 角色带图直接 400，图片只能进 `user` 消息（`agents/vision.py:6-9` 记录的端点限制）。
- OpenAI 风格：assistant 的 `tool_calls` 后面必须紧跟**每个 id 各一条** `tool` 消息；中间插 `user` 会造成"有 `tool_call` 没 `tool_result`"，下一次请求直接被拒。

两条合起来，合法形态只有两种：

| 形态 | 消息序列 | 图能放哪 |
|---|---|---|
| **一轮一个 `tool_call`** | `assistant(tool_calls=[1]) → tool → user(带图)` | `tool` 之后追加的 `user` 是**合法的下一轮**（本批 result 已齐） |
| **一轮多个 `tool_call`** | `assistant(tool_calls=[N]) → tool × N → user(带图)` | 只能挂在**最后一条** `tool` 之后，中间一条都不能插 |

因此 §8.2 那条规则的正确定义是：**"每个 `tool_result` 后追一条带图 user 消息"只在"每轮一个 `tool_call`"时成立**；一轮多个时退化为"**每轮末尾一张图**"。现有实现本来就是后者：`inject_image_blocks` 把图片消息**追加到消息列表末尾**（`agents/vision.py:191` `return [*messages, context]`），从不在中间插入。也就是说，这个机制天生只支持"末尾一张"。

#### 8.9.4 三个方案对比与取舍

| | A：`act(actions=[...])` | B：每轮一动作 | C：多 `tool_call` 靠调度器串行 |
|---|---|---|---|
| 串行性 | **天然**（工具内 for 循环） | **天然**（一轮只有一个） | **不天然**，靠 `max_concurrency=1`，且只在同步路径生效 |
| 往返次数（N 步，批长 M） | ⌈N/M⌉ | N | ⌈N/M⌉ |
| 截图张数 | ⌈N/M⌉（每批末尾 1 张） | N（每步 1 张） | ⌈N/M⌉（只能挂末尾） |
| 失败恢复 | **好**：内部"首错即停"，逐动作状态回传，未执行项标 `skipped` | **最好**：每步都验，立刻改 | **最差**：N 条一次派发，动作 1 失败后 2..N **照样执行**（CUA 里等于盲点） |
| 主要风险 | 批内"盲飞"（看不到中间态） | 成本与延迟最高 | 换异步路径后静默退化；顺序无契约保障 |
| 对 §8.2 规则 | 成立（一轮一个 `tool_call`） | 成立 | **不成立**（只能末尾一张） |

以 N=20、M=3 计【计算】：A/C 约 7 轮、7 张图；B 为 20 轮、20 张图。单张图的 token 数本身仍是【未验证】（§8.6 第 4 项），所以这里只比"张数与轮数"，不折算成钱。

**取舍：**

- **C 排除。** 它付出与 A 相同的成本，却丢了 A 的"首错即停"——对 CUA 这是安全性问题不是效率问题（一次盲点可能点掉一个对话框）；还额外依赖一个"换异步就失效"的隐式条件。
- **A 为主，B 为补充。** `act` 做默认通道；需要逐步观察或不确定性高的场景，模型少放几个动作、或直接调 `screenshot`，**等价于自动退化成 B**。这样不需要两套运行时，只是同一个工具的不同用法。

**`act` 的建议接口（未实现，仅登记）**：

```json
{
  "name": "act",
  "parameters": {
    "actions": [
      {"name": "click", "args": {"x": 640, "y": 360}},
      {"name": "type",  "args": {"text": "hello"}}
    ],
    "stop_on_error": true
  }
}
```

返回**纯文本**（图不在 `tool_result` 里）：`{"actions":[{"i":0,"name":"click","ok":true},{"i":1,"name":"type","ok":true}],"stopped_at":null,"image_ref":"<path>","width":2048,"height":1280,"origin":[0,0],"scale":1.5}`。运行时把 `image_ref` 还原成一条带图 `user` 消息**追加在末尾**，即 8.9.3 的形态 1。

四条实现约束：

1. **批长上限**：建议先取 3（Anthropic 也称之为 "a short sequence"），上限值属经验，需实测【未验证】。
2. **首错即停**：与官方一致；后续动作回 `{"ok":false,"skipped":true}`，**绝不"接着试"**。
3. **批内不允许嵌套 `screenshot`/`zoom`/`list_windows`/`session_state`**：观测类动作留在批外，否则 `act` 会变成"第二个循环"，工具内部再出现一个决策点。
4. **步数预算按物理动作计**，不按消息数：一个 `act` 带 5 个动作就是 5 步，否则硬上限会被"打包"绕过。

`act` 是**调度层工具（第 13 个）**，不推翻 §8.2 的 12 个原语——每个成员工具仍可单独调用，`act` 只是把它们按顺序串起来。

#### 8.9.5 没看到图的那几步，模型凭什么继续

一句前提：**在 A 方案下，"没看到图"只发生在批内，跨轮永远有一张图**。所以问题不是"模型盲走"，而是"模型在批内凭什么相信中间步骤成功了"。四条依据：

1. **动作语义是确定的，不靠图判断**：`click` 之后焦点就在被点的控件上，`type` 写入当前焦点。这是用户输入路径的既定行为，不是模型的猜测（§4.2 实测：点击/键入回读全部命中）。
2. **每个动作都有结构化返回值**：`{ok, at}`、`{ok, chars}`、`{ok, focus_hwnd}`。失败在返回值里就看得见，不需要看图。
3. **`act` 首错即停**：第一个失败点之后全部标 `skipped`，模型拿到的是一份"哪步断了"的精确清单，下一轮直接从断点重规划。
4. **批末强制一张图**：跨轮观测不缺失。这既是 Anthropic 的默认形状（"typically finishes a batch with `screenshot`"），也和我们"图只能挂末尾"的通道约束一致。

**代价要写清楚**：批内如果发生模型没预料到的事（弹窗、慢加载、焦点被抢），模型**当轮看不见**，只能靠批末那张图发现并回滚。缓解手段是批长上限 + 批内允许 `wait` + 批末必看图；**批内意外的实际发生率没有任何数据【未验证】**，属于 P0 闸门那一批一起测的内容。

**截图节奏定稿**：**每批末尾必有一张，且只有一张**；不按固定"N 步一取景"，节奏由批长决定——批长 1 就是每步一张（B），批长 3 就是三步一张。

**本节依据**

- 本机实测：`docs/CUA_DESKTOP_FEASIBILITY.md` §1（显示）、§3（DPI）、§4（注入）、§5（权限/窗口枚举）
- 管线代码：`agents/vision.py:49`（2048 上限）、`:90-99`（BICUBIC + JPEG q75）、`:6-9`（tool 角色带图 400）、`:191`（图片消息只追加到末尾）
- Anthropic 官方：`https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool`（17 个成员、坐标语义、zoom、提示词顺序；**Batch actions 节**用于 §8.9.1）
- OpenAI 官方：`https://platform.openai.com/docs/guides/tools-computer-use`（仅 Responses API，兼容网关接不上）——这是选 Anthropic 形态而非 OpenAI 的原因
- 调度器源码：`langgraph 1.2.20` `langgraph/prebuilt/tool_node.py:829-832`（`executor.map`）、`:863-866`（`asyncio.gather`）；`langchain-core 1.7.6` `runnables/config.py:672-675`（`max_workers=config.get("max_concurrency")`）；`langchain 1.4.9` `agents/factory.py:1140-1148`
- 调度器实测：本机 `feishu` 环境临时探针（未入库、未进 `docs/`，脚本在 `%TEMP%\toolnode_probe.py`；3 个 `sleep(0.3)` 工具，一条 AIMessage 三个 `tool_call`）：默认 **0.323 s / 3 线程**，`max_concurrency=1` **0.905 s / 1 线程且按序**

### 8.10 首轮实现与实测记录（2026-11-14）

按本节施工图落地的代码（只做单屏、纯视觉、12 原语 + `act`，未引入 ref/DOM/a11y）：

| 文件 | 内容 |
|---|---|
| `cua/dpi.py` | 进程启动即 `SetProcessDpiAwarenessContext(-4)`，只声明一次，不做事后纠正 |
| `cua/_win32.py` | ctypes 绑定（GDI 截图、VID 输入结构、屏幕/虚拟桌面指标） |
| `cua/screen.py` | 整屏抓取 → 长边 2048 → JPEG q75；`scale = 物理长边/2048`；zoom 按整图 region 裁剪并 1:1 返回 |
| `cua/input.py` | SendInput 绝对移动/点击/拖拽/滚轮/Unicode 键入；`key` 发送前查 IME |
| `cua/windows.py` | 窗口枚举、聚焦、锁屏/UAC 安全桌面检测 |
| `cua/session.py` | 动作状态机 + `act`（顺序执行、首错即停、批末一张图、物理动作预算）；截图按序号落盘、只留最近 N 张，连续同内容帧复用上一帧路径 |
| `cua/tools.py` | 13 个工具（文本 JSON + 独立文本块图片引用）；截图窗口配置钳制与告警 |
| `cua/browser.py` | 内核启动（`Settings.cua_browser_exe` 单点配置，不是模型工具） |
| `agents/task_kinds.py` / `agents/prompt.py` / `config.py` / `worker_runner.py` | `task_kind="cua"` 注册与提示词；CUA 配置段；按配置启停内核 |

【实测】几何与截图（本机 3072×1920，per-monitor V2）：模型空间 **2048×1280 / scale 1.5 / origin (0,0)**；单张 JPEG **174–332 KB**；整屏抓取+缩放+编码 **约 320 ms**；绝对移动 `delta=[0,0]`。

【实测】截图保留窗口（2026-11-14 追加）：`shot-000001.jpg` 起按序号落盘，只保留最近 `cua_screenshot_window` 张（默认 3，`.env` 可覆盖），窗口外文件即删；注入层按路径去重、按引用倒序取，缺失文件自动跳过，因此**一次请求恰好注入窗口内全部截图且不重复**。实测窗口 1/3/6 分别注入 1/3/6 张，顺序为旧→新，内容与磁盘一致；配置 9 被钳到 6 并告警，0 钳到 1，非法值退回 3。

【实测】图片 token（本机网关，2048×1280 JPEG）：单张请求 `input_tokens=1105`，3 张 `3133`，6 张 `6175`；**每多一张约 +1014 tokens**（与早期 643 的量级不同，按本轮实测为准）。

【实测】截图内容判重（2026-11-14 追加）：落盘侧把新 JPEG 的 `sha256` 与上一张**不同画面**比较，命中则不落新文件、复用上一帧路径，并在结果里回 `unchanged: true` / `duplicate_frames: n`；注入侧再按实际 data URL 的 sha256 去重，同一内容只保留**最新**那一帧的位置（A→B→A 注入 `[B, A]`，当前画面仍在最后），标题张数按实际注入数并注明"另有 k 帧完全相同，已合并"。窗口语义因此从"最近 N 次截图"变成"最近 N 个**不同**画面"。实测：同一张真实 JPEG 连拍 7 次，window=1/3/6 均**只注入 1 张**、`input_tokens=1106`；三张真实不同画面 `input_tokens=3166`。静态页 window=3 由上一版 3133 降到 1106（**-2027，约 -65%**）。

【未验证·限制】判重依据是**整屏字节相等**：本机真实桌面连拍 3 张全部不同（编辑器光标闪烁、任务栏等微变化），实际会注入 3 张。要按"视觉近似"判重需要阈值，容易把真实的小变化（勾选框、状态灯）一起吃掉，本轮不做，原样记录。

【实测】`act` 语义：批长 4 直接拒绝；批内坐标越界 → 该项失败、后续标 `{"ok":false,"skipped":true}`、`stopped_at` 指向断点；批末固定返回一张图，图片引用被 `agents.vision.inject_image_blocks` 识别并追加为末尾 user 消息（tool 结果里无图）。

【实测】端到端（真实模型 + Chrome 145，页面点击后跳转，每次跑用全新 profile）：`screenshot → act([click]) → 标题变为 E2E-AFTER`，整链路 **PASS**，窗口=3 时末次请求 `input_tokens` 依次 2517 / 3854 / 5302 / 6777。过程观察：首次 `click(913,687)` 只让**未聚焦**的 Chrome 获得焦点、页面未跳转，第二次 `click(933,700)+wait` 命中——属"未聚焦窗口吞掉首击"，不是定位误差；`drag` 等其它原语未因此重测。

【实测】输入原语回读（自有测试页，`document.title` 只作离线 ground truth）：`type("Hello 中文 123")` 回读一致（24 个事件）；`scroll(down, 5)` → `scrollY=500`；`zoom(0,0,600,400)` → 输出 900×600（物理 1:1，坐标仍按整图空间）。

【实测·观察，未定论】批末截图紧跟在动作后立即抓取：点击触发导航时，抓到的仍是**跳转前**的画面，模型靠自己补拍一张才发现跳转。代价是 **+1 次往返**；是否在批末加固定 settle 延时【未验证】，本轮按施工图未加。

【未验证·原样记录】
- 坐标命中率：本轮只有 **1/1** 个点击样本，**不构成命中率结论**；§8.6 的 P0 闸门仍未过。
- `drag`：实现已落地（按下→插值移动→松开），本轮**未做命中验证**。
- `key` 的 IME 分支：本轮前台窗口无 IME 上下文（`ImmGetContext` 返回 0），只验证到"未拦截时正常发送"，**`ime_intercept` 分支未经真机触发**。
- 2048 宽下读小字：未测；`zoom` 的实用性同样未测。
- 批长 1/2/3 的可靠性差异：无数据。

