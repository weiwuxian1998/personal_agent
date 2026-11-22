# CUA 无障碍树（UIA）导读、实测与案例

> 面向零基础：先讲清"UIA 是什么、怎么读"，再给**本机实测数据**，然后给已有实现案例（论文 + 工程），最后给本项目的接入设计。
> 本文不改动任何现有代码。
>
> 标注约定：**【实测】**= 本机 `scripts/probe_uia.py` / `scripts/exp_uia_cache.py` 跑出来的；**【官方】**= Microsoft 文档原文；**【论文】**= 原文明示；**【未验证】**= 没有数据，不做推断。
> 测试环境：Windows 11，3072×1920 @200%，进程声明 per-monitor V2。

---

## 0. 一分钟结论

| 问题 | 答案 |
|---|---|
| UIA 是什么 | Windows 的**无障碍树**：系统级、跨进程的一棵控件语义树，每个节点有角色、名字、包围盒和可执行的动作模式 |
| 和截图什么关系 | 互补。截图回答"长什么样"，UIA 回答"这是谁、在哪、能干什么" |
| 最大价值 | **精确寻址**（系统给的坐标，不靠模型 grounding）+ **可直接读文本** + **语义动作**（`SetValue` 零键盘注入） |
| 最大限制 | **不是所有程序都有树**。Qt/Java/自绘 canvas 接近零，Chromium/原生控件上很好 |
| 最大坑 | 逐属性读是**跨进程调用**（实测 7.7 ms/节点）；必须用 `CacheRequest` 批量取（实测快 3–6.8 倍） |
| 对本项目 | 值得做，但只能做**加分通道**，不能替换纯视觉主感知 |

> **需要订正的旧结论**：`docs/CUA_PURE_VISION_RESEARCH.md:647` 写"**桌面无 a11y 树可读**"，据此论证"页面级寻址不能扩到桌面"。这句在 Windows 上错了——UIA 本身就是桌面级无障碍树，且跨进程。应改为"页面级**工具集**不能扩到桌面，但**无障碍树这个数据源**在 Windows 桌面上是存在的"。

---

## 1. UIA 是什么（零基础）

### 1.1 一句话

UI Automation（UIA）是 Windows 从 Vista 起内置的**辅助功能框架**。它把每个窗口内部的控件抽象成一棵树，对外暴露节点**是什么**（角色/名字/状态）、**在哪**（包围盒）、**能干什么**（动作模式）。

它本来的目的是给屏幕阅读器（NVDA、讲述人）读屏——所以叫"无障碍"。但对 Agent 更要紧的性质是：**它是操作系统提供的、跨进程的、语义化的 UI 索引**。不需要注入目标进程、不需要 hook、不需要图像识别，就能问系统"这个窗口里有哪些按钮、坐标是多少"。

### 1.2 血缘：三代技术

| 代次 | 名称 | 特点 | 今天状态 |
|---|---|---|---|
| 1 | MSAA（Microsoft Active Accessibility） | `IAccessible` COM，角色/名字/位置，能力弱 | 仍在，经 UIA 的 `LegacyIAccessible` 模式桥接 |
| 2 | UIA | 原生 COM，引入 Pattern（动作模式）与 Condition（查询） | 主线 |
| 3 | UIA3 / IAccessible2 / Java Access Bridge | UIAutomationCore 统一客户端；浏览器走 IA2，Java 走 JAB | 现代应用的现实 |

**关键认知**：客户端看到的是**统一视图**，但底层 provider 五花八门——Win32 老程序走 MSAA 桥，WPF 走原生 UIA，Chromium 走 IAccessible2（再被 UIA 桥一层），Java 走 JAB。**桥接层决定信息损失多少**，这解释了为什么有的窗口树"很完整"、有的只有几个空壳节点（见 §3.3）。

### 1.3 对象模型：五件套

| 概念 | 含义 | 类比 |
|---|---|---|
| **AutomationElement** | 树上的一节点 = 一个控件 | DOM element |
| **ControlType** | 角色，46 种固定枚举（Button/Edit/ListItem/Document/Pane…） | `role` 属性 |
| **Property** | 属性，每个有固定 ID（Name=30005、BoundingRectangle=30001、IsEnabled=30010…） | 节点属性 |
| **Pattern** | 节点**能执行的动作**，按能力分组 | 方法集 |
| **TreeWalker + View** | 怎么遍历 | `querySelectorAll` 的等价物 |

**Pattern 是最有价值的部分**，它把"点击"变成"调用语义"：

| Pattern | 能力 | 对 Agent 的意义 |
|---|---|---|
| `InvokePattern.Invoke()` | 按下按钮 | 不用点坐标，不怕遮挡、不怕 DPI 偏移 |
| `ValuePattern.SetValue(s)` | 直接把值写进输入框 | **绕开键盘注入，中文输入法吞键问题直接消失**（§3.5 已实测） |
| `TogglePattern.Toggle()` | 勾选/取消 | 幂等，不用判当前状态 |
| `SelectionItemPattern.Select()` | 选中列表项 | 不用算行高 |
| `ExpandCollapsePattern.Expand()` | 展开下拉/树 | 不用模拟点箭头 |
| `TextPattern.GetText()` | 读出文本 | 长文/表格不用截图放大 |
| `ScrollPattern.Scroll()` | 滚动 | 不依赖滚轮坐标 |

### 1.4 三棵树：Raw / Control / Content

| 视图 | 内容 | 节点量 | 适合 |
|---|---|---|---|
| **RawView** | 全部节点，含布局用空 Pane | 最多 | 几乎不用 |
| **ControlView** | 有交互语义的控件 | 中 | 寻址（点按钮） |
| **ContentView** | 内容与信息节点，去装饰 | 略少 | 阅读（读文本/表格） |

**【实测】两者几乎一样多**：WorkBuddy 窗口 ControlView 477 节点 / ContentView 476 节点；资源管理器 191 / 171；Terminal 22 / 15。也就是说**换视图省不了多少 token**，别指望它当裁剪手段——裁剪得靠过滤规则（§6.4）。

---

## 2. 怎么读到它

### 2.1 接口层

```
IUIAutomation          ← 工厂 + 根节点 + TreeWalker + Condition + CacheRequest
  └─ IUIAutomation2    ← 加超时控制（TransactionTimeout）
  └─ IUIAutomation3    ← 文本编辑相关
  └─ IUIAutomation6    ← 事件与连接管理（Win10+）
IUIAutomationElement   ← 节点；GetCurrentPropertyValue / FindAll / GetCurrentPatternAs
IUIAutomationTreeWalker← GetFirstChildElement / GetNextSiblingElement
IUIAutomationCacheRequest ← 批量属性请求（性能关键，§2.2）
IUIAutomationCondition ← CreatePropertyCondition / And / Or / Not
```

| 项 | GUID |
|---|---|
| `CLSID_CUIAutomation` | `{FF48DBA4-60EF-4201-AA87-54103EEF594E}` |
| `IID_IUIAutomation` | `{30CBE57D-D9D0-452A-AB13-7AC5AC4825EE}` |
| `IID_IUIAutomationValuePattern` | `{A94CD8B1-0844-4CD6-9D2D-640537AB39E9}` |
| `IID_IUIAutomationInvokePattern` | `{FB377FBE-8EA6-46D5-9C73-6498B9F7B30E}`【未逐一实测】 |

**线程要求**（【官方】原话）：调用 UIA 前必须在该线程 `CoInitializeEx`；"If your client application might try to find elements in its own user interface, you must make all UI Automation calls on a **separate thread**." —— **这条对我们直接成立**：Agent 进程自己也有 UI，若在同一线程上查自己，会死锁/超时。

### 2.2 两条读取路径，性能差 3–7 倍

这是整件事最核心的工程判断：

| 路径 | 做法 | 跨进程往返 |
|---|---|---|
| **逐属性读** | 每节点每属性一次 `GetCurrentPropertyValue` | 节点数 × 属性数（200 × 14 ≈ 2800 次） |
| **缓存批量读**（正解） | `CreateCacheRequest` 声明要哪些属性 → `FindAllBuildCache` 一次取回 → 之后读缓存 | **1 次** |

【官方】对这个问题的描述和我们实测完全对上：

> "A client could retrieve individual properties one element at a time… However, this method is **slow and inefficient because it requires a cross-process call for each property being retrieved**. To improve performance, clients can use the caching (also called bulk fetching) capabilities."

**三个必须知道的细节（都是【官方】，且都有实际后果）**：

| 细节 | 后果 |
|---|---|
| 缓存请求的 scope 默认是 `TreeScope_Element`（只缓存返回元素自身） | **正好是我们要的**，别去改它（改错会静默 E_FAIL，见 §3.4 坑 5） |
| `AutomationElementMode_None` = 只拿缓存数据，**不能再 `GetCurrentPattern`/`Invoke`** | 读和动要分开：读用 None 模式省资源，动必须用完整引用 |
| "There is **less benefit** when client-side providers, such as the default providers for **Win32** controls, are accessed" | Win32 窗口（资源管理器）加速比偏低是预期内的，不是实现问题 |
| 遍历桌面根节点要用 `TreeScope_Children` 而不是 `Descendants` | 否则"可能遍历数千项导致栈溢出"——直接崩 |

### 2.3 事件模型：知道有，别用在 agent 主循环

UIA 支持订阅焦点/属性/结构变化。对 Agent 代价大于收益：回调在 UIA 线程池触发，容易和动作执行交错，且很难做成"等这个按钮出现"的同步语义。**轮询（重新快照）比事件稳。**

### 2.4 坐标与 DPI：一个必须验证的隐性前提

| 事实 | 说明 | 证据 |
|---|---|---|
| 包围盒**跟随调用方进程的 DPI 感知级别** | 同机同窗口：DPI unaware 进程读到 1536 宽，per-monitor V2 进程读到 3072 宽 | 【实测】同一 Chrome 窗口：unaware → `1536×912`；声明后 → `3072×1824` |
| 好消息：前提已经满足 | `cua/dpi.py:84` 模块导入即声明 per-monitor V2，所以**进程内**读到的包围盒就是**物理像素**，与 `screen.Geometry.phys_w/phys_h` 同空间 | 同上 |
| 所以坐标换算零成本 | `model = physical / geom.scale`，即现成的 `Geometry.to_model()`，不需要第二套坐标系 | — |
| 但跨进程要重来 | 若 UIA 放子进程/未初始化的线程，会差 2× 且**不报错** | 同上 |

---

## 3. 本机实测

### 3.1 为什么用纯 ctypes（两个脚本，零依赖）

`scripts/probe_uia.py`（看树）+ `scripts/exp_uia_cache.py`（做 A/B 实验），都不依赖 `uiautomation` / `comtypes` / `pywin32`。原因不是炫技：

1. 项目 `requirements.txt` 里没有任何 UIA 依赖，环境里也确实没有【实测：import 全 MISSING】；
2. 探针要能在"决定做不做之前"跑，装依赖本身就是个决策；
3. 这套 vtable 调用骨架，就是将来真要落地时最小实现的雏形。

### 3.2 覆盖分布：不是所有程序都有树

`python scripts/probe_uia.py --all`，上限 400 节点 / 8 层【实测】：

| 窗口 | 框架 | 节点 | 遍历耗时 | 有名字 | 有包围盒 |
|---|---|---|---|---|---|
| WorkBuddy | Chromium/Electron | 186 | 1435 ms | 84% | 91% |
| 千问控制台（浏览器页） | Chromium | 61 | 230 ms | 65% | 95% |
| 文件资源管理器 | Win32/Shell | 191 | 663 ms | 86% | 96% |
| Windows 设置 | WinUI/XAML | 139 | 355 ms | 83% | 70% |
| Windows Terminal | XAML/Cascadia | 22 | 57 ms | 72% | 90% |
| 网易UU远程 | Qt 5.15 | 81 | 141 ms | **17%** | 100% |
| 某业务客户端 | Qt 5.15 | 45 | 99 ms | **28%** | 100% |
| **微信** | Qt 5.15 | **4** | 30 ms | — | — |
| 某 Java 应用 | AWT/Swing | **8** | 39 ms | 75% | 62% |

**读法（比任何结论都重要）**：

- **Chromium 系（Chrome/Edge/Electron）与 Explorer/WinUI 很好**：80%+ 有名字、90%+ 有包围盒，够用；
- **Qt 系是重灾区**：要么节点极少（微信 4 个），要么一堆无名 Group（UU远程 81 个只有 14 个有名字）——**有包围盒但不知道该点哪个**；
- **Java 几乎为零**：没有 JAB 桥就是 8 个节点；
- 同框架差异巨大（UU远程 81 vs 微信 4）→ **不要按框架预判，要按窗口实测**。

### 3.3 token 成本：省不了钱，但买到精确

把树序列化成紧凑行（`角色 "名字" @cx,cy`）【实测】：

| 裁剪策略 | 行数 | 字符 | 约 token |
|---|---|---|---|
| 全量 | 208 | 6145 | 2332 |
| 去无名 + 去 offscreen | 153 | 4811 | 1860 |
| 再去掉与父级重复的 Text | 117 | 3765 | **1467** |

**意义**：2048 宽的整屏截图与这个量级相当，所以"UIA 更省 token"作为**主论点不成立**。真收益在别处：精确、可读、可动作（§0 表）。

文本量实测（ContentView，`name` 字段累计）【实测】：Qoder 窗口 32814 字、WorkBuddy 4351 字、资源管理器 924 字 + value 1267 字。**读文档类任务从这里拿文本，比 `zoom` 截图让模型认字便宜得多。**

### 3.4 八个坑，全是静默出错

| # | 坑 | 现象 | 正解 |
|---|---|---|---|
| 1 | **`UIA_BoundingRectangle` 不是 RECT** | 返回 `double[4]` = **(left, top, width, height)**。按 RECT 解会得到 `right < left` 的"合法"数字（如 `Text @[104,7,28,16]`） | 按 (x,y,w,h) 解；中心 = `(x+w/2, y+h/2)` |
| 2 | **DPI 感知级别决定坐标系** | 未声明 DPI 的进程读逻辑像素（差 2×），无任何报错 | 进程启动即声明；**并校验**根窗口宽 ≈ `phys_w` |
| 3 | **`IsXXXPatternAvailable` 不可信** | Chrome 给纯 Text 节点也报 `Invoke` 可用（"可交互 99%"毫无区分度） | 别用它裁剪；要动作就 `GetCurrentPatternAs` 拿真接口 |
| 4 | **逐属性跨进程，7.7 ms/节点** | 186 节点 Chrome 窗口 1435 ms | `CacheRequest` + `FindAllBuildCache`（实测 3–6.8×） |
| 5 | **给 cache request 设 `TreeScope` 会让 `FindAllBuildCache` 直接 E_FAIL** | 返回 `0x80004005`，不告诉你为什么 | **不要设**：默认 `TreeScope_Element` 正是要的；搜索范围用 `FindAllBuildCache` 的入参控制 |
| 6 | **Pattern 的 IID 记错 → `E_NOINTERFACE` (0x80004002)** | 明明 `IsValuePatternAvailable=True`，就是拿不到接口 | ValuePattern = `A94CD8B1-0844-4CD6-9D2D-640537AB39E9` |
| 7 | **`IUIAutomationValuePattern` 的 vtable 顺序和直觉相反** | `SetValue` 在索引 **3**（不是最后），`IsReadOnly` 在 5 | 顺序：3 SetValue / 4 get_CurrentValue / 5 get_CurrentIsReadOnly |
| 8 | **`ControlType=Edit` ≠ 可写** | 资源管理器文件项也是 `Edit`，但 `ValuePattern` 根本不存在（IsReadOnly 都问不到） | 判可写要 `IsValuePatternAvailable` + `IsReadOnly`，别用 ControlType 猜 |

### 3.5 E1/E2 实验结果：缓存到底快多少

`python scripts/exp_uia_cache.py --all`【实测，per-monitor V2】：

| 窗口 | 节点 | A 逐属性 | B 缓存 | 加速 |
|---|---|---|---|---|
| WorkBuddy（Chromium） | 477 | 2277 ms | 452 ms | **5.0×** |
| Qoder（Chromium） | 871（A 被上限截到 600） | 1584 ms | 341 ms | **4.6×** |
| 千问控制台（Chromium） | 68 | 302 ms | 59 ms | **5.1×** |
| 资源管理器（Win32） | 191 | 642 ms | 200 ms | 3.2× |
| 网易UU远程（Qt） | 115 | 214 ms | 32 ms | **6.8×** |
| 某业务客户端（Qt） | 45 | 89 ms | 26 ms | 3.4× |
| Windows Terminal | 22 | 63 ms | 28 ms | 2.3× |
| Java AWT | 8 | 40 ms | 28 ms | 1.5× |

**三条结论，都跟直觉不一样**：

1. **加速比 3–7 倍，不是一个数量级**。我之前按"合并成 1 次调用"推断会快 10× 以上，实测打脸——剩下的 200–450 ms 主要花在那一次 `FindAllBuildCache` 本身（Chrome 构建整棵树就慢）；
2. **绝对耗时仍然是几百毫秒级**。所以"snapshot 能不能进主循环"仍是真问题，不是优化完就没事了（§6.4 约束 1）；
3. **节点越多收益越大，但小树有固定开销**（Java 8 节点只快 1.5×）——给"小树"单独优化没意义，反而该走覆盖阀直接放弃。

### 3.6 E3/E4：VaulePattern 实测能绕开输入法

`python scripts/exp_uia_cache.py --setvalue`【实测】：

```
=== E4  ValuePattern.SetValue（零键盘注入，绕开输入法）===
  找到 Edit（深度 2）IsValuePatternAvailable=True
  IsReadOnly=False
  SetValue hr=0x00000000
  读回 = 'UIA SetValue 测试：中文与符号 ￥%&*()'  [一致]
```

这是对 `CUA_PURE_VISION_RESEARCH.md:802` **未解决项 #1（中文输入法吞 VK 按键）**的直接答案：**完全不走键盘**，值由 provider 直接写入，中文/全角符号/特殊字符全部精确一致。

代价是**不是所有输入框都有这个能力**：Win11 的记事本输入区是 WinUI 的 `Document`（没有 ValuePattern）；Explorer 的文件项也是 `Edit` 但没有 ValuePattern。所以它是**快路径，不是唯一路径**，必须有降级。

---

## 4. 为什么有的程序没有树

树是**目标程序自己实现的**（provider 侧）。不实现，系统就造不出来。

| 程序类型 | provider | 实测表现 | 原因 |
|---|---|---|---|
| Win32 标准控件 | 系统内置代理 | 好 | 系统代实现 |
| WPF / WinForms / WinUI3 | 原生 UIA provider | 好 | 框架自带 |
| Chromium（Chrome/Edge/Electron） | IAccessible2 → UIA 桥 | 好，但**首次慢** | 懒启用：检测到 UIA 客户端才构建树，第一轮要等 |
| Qt | 需 Qt 无障碍插件 | **差** | 默认不开或只暴露粗略分组 |
| Java（Swing/AWT） | 需 Java Access Bridge | **极差** | 需单独启用 `jabswitch` 并加载桥 |
| 自绘 / Canvas / 游戏 / 远程桌面画面 | 无 | **零** | 客户区就是一张像素表面，没有控件概念 |
| 提权进程（管理员窗口） | — | 读不到/点不动 | UIPI：低完整性进程看不到高完整性进程窗口内部 |

> **判断规则**：一个窗口只要"内容都画在同一块 surface 上"（远程桌面、K 线图、游戏、部分老旧金融终端），UIA 永远帮不上——只能纯视觉。这决定了 UIA 在本项目**只能是加分项**。

---

## 5. 已有实现案例

### 5.1 四种混合形态（先看这张，再看论文）

把"树 + 图"的结合方式归一下类，比逐篇读论文省事。**没有一家是只靠树的。**

| 形态 | 树的作用 | 图的角色 | 代表 |
|---|---|---|---|
| **① 纯视觉** | 无 | 唯一感知 | OmniParser（arXiv:2408.00203）；**本项目现状** |
| **② SoM 标注** | 只用来**画编号框**，结构不入模型 | 唯一感知载体 | Windows Agent Arena / Navi |
| **③ 树为主 + 图增补** | 树打唯一 ID 做**寻址**；OCR 文本块**反向补进树**（IoU 去重） | 观察弹窗/按钮状态/验证上一步是否生效 | **Agent S** |
| **④ 双检测融合** | UIA 出结构化元数据；视觉模型出漏检元素，**转成"伪 UIA 对象"** 合成统一表示 | 补 UIA 覆盖不到的 | **UFO²** |
| **⑤ 语义指针** | `ElementFromPoint` 把坐标**翻译成语义元素**（角色/名字/状态） | 视觉上下文 + 蓝图 | **LUMOS** |

### 5.2 论文

| 工作 | 出处 | 做法 | 可借鉴的点 / 数字 |
|---|---|---|---|
| **UFO** | arXiv:2402.07939（Microsoft，2024） | `Control Interaction` 模块把动作翻译成 **UIA / Win32 两种后端**；带 control filtering 去噪 | 最早的桌面控件寻址工程化；双通道起点 |
| **UFO²** | arXiv:2504.14603（Microsoft，2025） | UIA 元数据 + OmniParser-v2（YOLO-v8 + 微调 Florence-2）；**IoU 去重**，未与 UIA 重叠的视觉检测**转成伪 UIA 对象**；GUI–API 混合动作（能调 COM/xlwings 就不点 UI） | 目前最完整的答案：**统一到一种数据结构**。论文称混合把最高 **9.9%** 的失败任务转为成功 |
| **Agent S** | arXiv:2410.08164 | **双输入**：图看"细枝末节"（弹窗、状态、上一步是否生效），树用来推理 + **grounding**（每元素打整数 tag，动作写 `click(element_id)`）；**树被图增补**（OCR 文本块按 IoU 补进树）；约束动作空间、每步一动作 | 与我们结构最像的参考。OSWorld 全套 369 任务：**20.58%**，比最佳基线（11.21%）**接近翻倍**；也在 WAA 154 任务上跑过 |
| **Windows Agent Arena** | arXiv:2409.08264（Microsoft，2024） | UIA 树只用于生成 **Set-of-Marks**（画编号框） | 关键量化：加 UIA 标注后 GPT-4V **12.5% → 19.5%（相对 +57%）**；对开源检测模型 +52%；最佳配置仍仅 19.5% vs 人类 74.5%。**并明确警告**："UIA markers can take **from a few seconds up to several minutes** to be queried depending on screen complexity" → 正是 §3.5 那个问题 |
| **VistaGUI** | OpenReview（ICLR 2026 投稿） | **无障碍树为主**，视觉/OCR 只对"未解析区域"**局部放大**；配回滚-纠错-续跑 | 混合感知的"够用就好"范式。报告 WAA **32.23%**（比 Operator 20.8% 高 11.43 个绝对点），OSWorld-W 22.4% |
| **LUMOS** | arXiv:2606.30697 | 在 OS 之上做"语义层"：产出带**稳定元素 ID** 的 blueprint（原生 A2 / 网页 W3），LLM 每轮只输出一个 schema 约束的 JSON 动作；`ElementFromPoint` 做**语义指针** | 两个设计值得抄：**稳定 ID 命名**、**动作集中含 `set_text`**（对应 ValuePattern），并把 `finish` 作为显式动作 |
| **OmniParser** | arXiv:2408.00203 | 纯视觉：检测模型 + 图标描述模型，把截图解析成结构化元素 | 对照基线。值得记住它的定位不是"替代树"，而是"树没有时的兜底" |
| **A11y-CUA** | CHI 2026 数据集 | 采集桌面任务时同步存 **UIA 树快照（截到深度 4，含 name/control_type/automation_id/bounding_rectangle）** + 事件流 | 直接给了"树序列化该长什么样"的工业口径：**深度 4 + 四字段** |
| **DeskForge** | arXiv:2610.02320（ETH/IBM） | 用**无障碍树 + 窗口几何 + 截图**三源同步，自动生成稠密标注（120 万观测 / 1.597 亿元素实例） | 说明树的另一个用途：**当标注真值**，不一定要进 prompt |
| **GUI Agents 综述** | arXiv:2411.18279 / ACL 2025（2412.13501） | 把感知分三类：截图 / DOM-a11y 解析 / 混合（SoM） | 选型框架，可直接引用 |
| **Efficient GUI Agents 综述** | arXiv:2609.02309 | 把 a11y 树定位为 "observation enrichment"：**紧凑但往往不完整、有噪声、与渲染不对齐** | 与我们 §3.2/§4 的结论一致 |

**三个跨案例的共同结论**（最有价值的部分）：

1. **没人只靠 UIA**。所有 Windows 原生 Agent 都是树 + 视觉混合，理由一致：覆盖不全；
2. **混合的接口是"统一控件对象"**，不是两套工具。UFO² 甚至把视觉检测**降格转成伪 UIA 对象**，让下游只认一种数据结构；
3. **UIA 的价值被量化过**：相对提升 +15% ~ +57%，量级取决于视觉基线有多弱。

### 5.3 工程实现

| 项目 | 形态 | 做法 | 注意 |
|---|---|---|---|
| **microsoft/UFO**(+UFO²) | Python + C#（UFO² 自称 3 万+行） | HostAgent + AppAgent 双层；混合检测；GUI–API 混合动作 | "能调 API 就别点 UI"写成优先级；PiP 用 RDP loopback 隔离桌面 |
| **CursorTouch/Windows-MCP** | Python，MCP server（~1.2 万 star，MIT） | `State-Tool` 一次返回：活动应用 + 可交互/文本/可滚动元素（**来自 a11y 树**）+ 截图；浏览器有 `use_dom=True` 过滤浏览器外壳 | 最接近"snapshot"的现成形态。README 自陈 Firefox 走 **IAccessible2 fallback**（不暴露 RootWebArea）；动作延迟自报 0.2–0.5 s |
| **simular-ai/Agent-S** | Python（`gui_agents`） | `observation_type="mixed"`，观测里同时带 `screenshot` 与 `accessibility_tree`；Windows/macOS/Linux 各一个 ACI | 可直接读源码看它怎么把树序列化成 tag + 怎么回填 OCR 文本 |
| **yinkaisheng/Python-UIAutomation-for-Windows** | Python（纯 ctypes 薄封装） | 直接包 `IUIAutomation` | 社区验证最广；有人写 ADR 论证"要看清树就用薄封装，别用带等待/重试语义的自动化框架" |
| **pywinauto** | Python | 双后端 `win32` / `uia` | 适合"驱动应用"，不适合"如实 dump 树"——自带重试会掩盖真实行为 |
| **FlaUI** | .NET | 同时支持 UIA2 / UIA3 | 将来若用 C#/C++ 做更快的集成，首选 |
| **Accessibility Insights for Windows** | 微软官方工具 | 可视化树 + 悬停高亮包围盒 + 属性面板 | **排查"这个窗口到底有没有树"的第一工具**，比写代码快 |

---

## 6. 如果接进本项目（只讲设计）

### 6.1 定位

| 判断 | 理由 |
|---|---|
| **不做主感知层** | §3.2/§4 的覆盖率决定了：微信、Qt 客户端、Java 客户端上等于没有，而这些恰恰是桌面任务的常见对象 |
| **做"按需精确视图"** | 模型怀疑坐标不准、要读长文、要填输入框时，主动要一份树 |
| **做"语义动作"通道** | 有 `ValuePattern`/`InvokePattern` 就别注入键鼠——§3.6 已实测能绕开 IME |

一句话：**截图是眼睛，UIA 是手和尺子。**

### 6.2 三种形态（按收益/成本排序）

| 形态 | 做法 | 收益 | 成本 |
|---|---|---|---|
| **A. 观察增补** | 新增观测动作（类比 `screenshot`/`zoom`），返回裁剪后的元素表（角色/名字/包围盒转到 model 空间/ref） | 精确坐标 + 可读文本；批末仍回截图兜底 | 低：一个观测分支 + 裁剪 |
| **B. 双形状 target** | `click` 从"只收坐标"变成"收坐标或收 ref" | 定位确定、失败可解释 | 中：ref 表 + 失效校验 + 代次 |
| **C. 语义动作** | `type(ref,text)` 走 `SetValue`；`click(ref)` 优先 `Invoke` | 绕开 IME/遮挡/动画 | 中高：模式不可用要有降级 |

建议顺序：**A 先做（纯观测、零行为风险）→ 用真实任务验收益 → 再定 B/C。**

### 6.3 与现有代码的边界（只列锚点）

| 位置 | 语义 | 关系 |
|---|---|---|
| `cua/session.py:52` `OBSERVATION_ACTIONS` | 观测动作集合，不能进 `act` 批次 | snapshot 天然属于这类 |
| `cua/session.py:405` `dispatch` | 动作名 → handler 唯一映射 | snapshot 挂这里；B 是给 click 加可选入参 |
| `cua/screen.py:55` `Geometry.to_model` | 物理像素 → 模型空间 | UIA 包围盒直接喂它，**不需要第二套坐标系** |
| `cua/dpi.py:84` | per-monitor V2 | 坐标正确性前提，**已满足** |
| `cua/session.py:92` `RLock` | 动作串行化 | **冲突点**：UIA 几百毫秒到数秒，占锁会堵住 click/type |
| `cua/session.py:45` `max_batch` | 物理动作预算 | 观测动作当前不计预算，snapshot 是否算要明确 |

### 6.4 必须先定的六个约束

| # | 约束 | 为什么 |
|---|---|---|
| 1 | **锁的边界** | 实测单次 200–450 ms，WAA 说"几秒到几分钟"。共用 `RLock` 会让一次卡住的 snapshot 冻结整个会话 |
| 2 | **超时机制** | `IUIAutomation2.TransactionTimeout` + 调用侧兜底。无超时的 UIA 调用 = 不可控阻塞 |
| 3 | **裁剪规则** | 不裁剪就 2.3k token 起（实测），且噪声淹没信号。规则要可解释：View / 深度 / 有无名字 / offscreen / 与父重名 |
| 4 | **ref 生命周期** | ref 绑定"快照代次"；元素消失或包围盒变了就报失效要求重拍，**绝不猜** |
| 5 | **覆盖阀** | 节点过少或命名率过低时明确说"此窗口无 a11y 树，回退视觉"，别给空表让模型以为窗口是空的 |
| 6 | **坐标一致性自检** | 每份快照校验根窗口宽 ≈ `phys_w`；不等就说明 DPI/坐标系有问题，应报错而不是继续 |

### 6.5 验收指标（可量化）

| 指标 | 定义 | 基线 |
|---|---|---|
| 坐标命中率 | 3 类界面 × 10 目标，ref 寻址 vs 看图给坐标 | 纯视觉基线**尚无数据**（`:804` 的 P0 闸门），先测再比 |
| 快照耗时 P50/P95 | 单窗口一次 snapshot 墙钟 | 实测 200–450 ms（Chromium 大树），小树几十 ms |
| 快照 token | 序列化后 token | 实测 1467–2332，作为"必须裁剪"的证据 |
| 覆盖率 | 目标应用中有树且命名率 > 50% 的比例 | 先按 §3.2 方法把**财汇终端等真实目标**测一遍 |
| 输入法拦截率 | `type` 走 SetValue 后 `ime_intercept` 的下降 | 对应 `:802` 未解决项 #1 |

---

## 7. 风险清单

| 风险 | 触发 | 后果 | 缓解 |
|---|---|---|---|
| 遍历卡死 | 目标 UI 线程忙/模态阻塞 | snapshot 挂住，连带输入被堵 | 独立锁 + 超时 + 可放弃 |
| token 爆炸 | 深树/表格/长文档 | 一次几万 token | 强制上限 + 覆盖阀 |
| 静默坐标错位 | DPI 丢失 / 子进程未初始化 | 点击偏 2×，像"随机失败" | 根节点宽高自检 |
| ref 失效误点 | 页面重排后仍用旧 ref | 点错且无提示 | 代次校验 + 失效即报错 |
| 过度依赖 | 有树的窗口上模型不再看图 | 视觉退化，遇无树窗口就崩 | 快照始终附截图；提示词明确"图是权威" |
| 权限壁垒 | 目标以管理员运行 | 读不到/点不动 | 检测 UIPI 明确报错，不重试 |
| **查自己的 UI** | 同线程查 Agent 自身界面 | 死锁/超时【官方警告】 | UIA 调用放独立线程 |

---

## 8. 怎么自己动手（五分钟）

**看树（不写代码）**：装微软 **Accessibility Insights for Windows**，悬停即高亮包围盒并显示角色名。判断"有没有树"用它最快。

**看树（跑脚本）**：

```bash
python scripts/probe_uia.py                # 前台窗口
python scripts/probe_uia.py --all          # 所有可见窗口，出覆盖率表
python scripts/probe_uia.py --rows 40      # 前 40 个元素明细（角色/名字/包围盒/Pattern）
python scripts/probe_uia.py --content      # 用 ContentView（更适合读文本）
```

**做实验（A/B + 写入）**：

```bash
python scripts/exp_uia_cache.py --all       # 逐属性 vs 缓存，View 差异，文本量
python scripts/exp_uia_cache.py --setvalue  # 追加：运行框里零键盘注入写中文并读回
```

---

## 9. 参考资料

**官方文档**

- Caching UI Automation Properties and Control Patterns — <https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-cachingforclients>（"cross-process call for each property"、"less benefit for Win32"、`AutomationElementMode_None` 的后果、自身 UI 需独立线程）
- `IUIAutomationElement::FindAllBuildCache` — <https://learn.microsoft.com/en-us/previous-versions//dd319237(v=vs.85)>（桌面根要用 `TreeScope_Children`，否则可能栈溢出）
- `IUIAutomationValuePattern` — <https://learn.microsoft.com/en-us/windows/desktop/api/UIAutomationClient/nn-uiautomationclient-iuiautomationvaluepattern>（IID 与 vtable 顺序以头文件为准）

**论文**

- UFO — arXiv:2402.07939，<https://arxiv.org/abs/2402.07939>
- UFO²: The Desktop AgentOS — arXiv:2504.14603，<https://arxiv.org/pdf/2504.14603>；项目文档 <https://microsoft.github.io/UFO/ufo2/overview/>
- Agent S: An Open Agentic Framework that Uses Computers Like a Human — arXiv:2410.08164
- Windows Agent Arena — arXiv:2409.08264，<https://arxiv.org/abs/2409.08264>
- VistaGUI（ICLR 2026 投稿）— <https://openreview.net/forum?id=ld2bbwEvSu>
- LUMOS: A Semantic Operating-System Layer for Accessibility-Grounded AI Agents — arXiv:2606.30697
- OmniParser for Pure Vision Based GUI Agent — arXiv:2408.00203
- DeskForge — arXiv:2610.02320
- GUI Agents: A Survey（ACL 2025 Findings）— arXiv:2412.13501
- Large Language Model-Brained GUI Agents: A Survey — arXiv:2411.18279
- Efficient GUI Agents: A Systems Survey — arXiv:2609.02309，<https://arxiv.org/html/2609.02309>

**工程**

- microsoft/UFO — <https://github.com/microsoft/UFO>
- CursorTouch/Windows-MCP — <https://github.com/CursorTouch/Windows-MCP>
- simular-ai/Agent-S — <https://github.com/simular-ai/Agent-S>
- yinkaisheng/Python-UIAutomation-for-Windows — `pip install uiautomation`
- pywinauto — <https://github.com/pywinauto/pywinauto>；FlaUI — <https://github.com/FlaUI/FlaUI>
- Accessibility Insights for Windows — <https://accessibilityinsights.io/>

**本仓库**

- `docs/CUA_PURE_VISION_RESEARCH.md` §7.3（**647 行结论需订正**）、§8.6（#1 输入法吞键、#3 坐标命中率）
- `docs/CUA_BROWSER_RESEARCH.md` §11（a11y 快照 token：58 – 131,828）
- `scripts/probe_uia.py`（覆盖/token/坐标实测）、`scripts/exp_uia_cache.py`（A/B + SetValue 实测）
- `cua/dpi.py`、`cua/screen.py`、`cua/session.py`（坐标与锁现状）

---

*文档建立 2026-11-21，同日补入 E1–E4 实测。所有【实测】数字为本机 3072×1920 @200%、per-monitor V2 下测得；换机器/换目标需重测，不要直接引用。*
