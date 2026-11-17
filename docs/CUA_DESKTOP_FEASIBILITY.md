# 本机纯视觉桌面 CUA 可行性实测

> 对象：这台机器（Windows，2 屏，主屏 200% 缩放）。
> 日期：2026-11-13。本文**只写实测**，不写设计愿景；所有数字都可用文末命令复现。
> 约束遵守：只在自建窗口上操作；未改 DPI / 电源 / 注册表；未提交 commit；测试进程与窗口已销毁。

**证据标记**：
- **【实测】** 本机跑出来的数字，附脚本与产物路径
- **【API】** Win32/MSDN 行为定义
- **【未验证】** 无法在本机安全验证，只给检测方法

---

## 0. 一句话结论

**能跑，但只在一种配置下能跑：进程必须声明 per-monitor V2 DPI 感知 + 自建窗口实测点击误差 0–3 px、全屏截图 66 ms、键盘 Unicode 输入精确回读。**
不声明 DPI 的进程在这台机器上**只能看到屏幕左上角 25% 且完全不自知**，这是"看着对点不中"的真实机制。
真正的硬卡点不是截图也不是点击，而是三件小事：**DPI 声明、中文输入法吃掉按键、3072×3000 的原生分辨率必须缩到 819×800 才能进模型**（文字被缩 3.7 倍，可读性未验证）。

| 项 | 结论 | 关键数字 |
|---|---|---|
| 显示环境 | 可用，但混合 DPI（200% + 100%） | 虚拟屏 3072×3000，原点 (0,-1080)，负坐标真实存在 |
| 截图 | 可用 | 全屏 66.3 ms / 1.24 MB PNG；单主屏 50.9 ms |
| DPI | **唯一正确解：per-monitor V2** | 声明后偏移 0 px；不声明 = 只截到 1/4 屏、窗口尺寸 2× |
| 输入注入 | 可用 | 移动误差 0–3 px，点击/双击/右键/滚轮/Unicode 键入全部命中 |
| 键盘功能键 | **受阻** | 中文输入法把 VK 按键变成 `VK_PROCESSKEY(229)`，控件收不到 |
| 权限 | 中完整性、未提权 | 当前无高权限窗口，UIPI 实测【未验证】 |
| 锁屏 | 无法测 | 只能给检测信号，锁屏/断 RDP 均【未验证】 |

---

## 1. 显示环境

脚本：`cua-desktop-bench/probe_display.py`（`EnumDisplayMonitors` + `GetMonitorInfoW` + `GetDpiForMonitor` + `GetScaleFactorForMonitor` + `EnumDisplaySettingsW`）

### 1.1 实测枚举结果（per-monitor V2 进程）

| 设备 | 主屏 | 物理矩形（虚拟屏坐标） | 尺寸 | DPI | 缩放 | DEVMODE 原生 |
|---|---|---|---|---|---|---|
| `\\.\DISPLAY1` | 是 | `(0, 0) - (3072, 1920)` | 3072×1920 | 192 | **200%** | 3072×1920 @60 Hz, 32bpp |
| `\\.\DISPLAY2` | 否 | `(604, -1080) - (2524, 0)` | 1920×1080 | 96 | **100%** | 1920×1080 @59 Hz |

- 虚拟桌面：`SM_XVIRTUALSCREEN=0`，`SM_YVIRTUALSCREEN=-1080`，`SM_CXVIRTUALSCREEN=3072`，`SM_CYVIRTUALSCREEN=3000`。
- **副屏在 y 轴负方向**（放在主屏上方），所以任何"假设坐标非负"的实现都会崩。
- `GetDpiForSystem()=192`（系统 DPI 取自主屏，200%）。
- 工作区比屏幕少 96 px（`rcWork` bottom=1824），即主屏任务栏高 96 物理像素。

### 1.2 同一个 API 在三种 DPI 声明下的返回值（这是本次最有价值的一组数据）

| 声明 | `DISPLAY1` 矩形 | `DISPLAY2` 矩形 | 虚拟屏 | `GetDpiForSystem` |
|---|---|---|---|---|
| **per-monitor V2** | 3072×1920 | 1920×1080 @ (604,-1080) | 3072×3000 | 192 |
| **system aware** | 3072×1920 | **3840×2160 @ (1208,-2160)** | **5048×4080** | 192 |
| **unaware** | **1536×960** | 1920×1080 @ (604,-1080) | **2524×2040** | 96 |

读法：
- **system aware 在副屏上是错的**：副屏是 100% 缩放，但 system-aware 进程按主屏 192 DPI 换算，把它的矩形整体乘了 2 → 尺寸 3840×2160（虚报），位置 (1208,-2160)（虚报）。
- **unaware 在主屏上是错的**：3072×1920 被折算成 1536×960，屏幕看起来只有一半大。
- 只有 per-monitor V2 两个屏都对。

---

## 2. 截图实测

脚本：`cua-desktop-bench/shot.py`（mss 10.2.0 / PIL.ImageGrab / 自写 GDI BitBlt，各 5 次取中位数）

### 2.1 主表（per-monitor V2，屏幕内容为普通桌面）

| 目标 | 图像尺寸 | mss 抓取中位数 | 最小/最大 | PNG 字节 | PNG 编码耗时 | 平均亮度 |
|---|---|---|---|---|---|---|
| **虚拟屏整幅** | **3072×3000** | **66.3 ms** | 64.2 / 73.0 | 1,236,673 | 280.2 ms | 203.42 |
| 主屏 | 3072×1920 | 50.9 ms | 45.1 / 53.2 | 842,063 | 118.9 ms | 233.78 |
| 副屏 | 1920×1080 | 28.7 ms | 20.2 / 33.1 | 373,552 | 43.0 ms | 239.10 |

同尺寸换实现（虚拟屏整幅）：

| 实现 | 中位数 | 说明 |
|---|---|---|
| mss | 66.3 ms | 走 GDI BitBlt，可用 |
| `PIL.ImageGrab(all_screens=True)` | 82.6 ms | **三种 DPI 模式下尺寸都恒为 3072×3000**，是唯一"天然 DPI 安全"的实现 |
| 自写 `BitBlt` + `GetDIBits` | 76.8 ms | 需要自己管 DC/位图，未比 mss 快 |

### 2.2 传模型前的编码成本

`cua-desktop-bench/encode_bench.py`，源图 3072×3000：

| 变体 | 尺寸 | 字节 | base64 后 | 编码/缩放耗时 |
|---|---|---|---|---|
| PNG 原尺寸 | 3072×3000 | 1,236,673 | ~1.65 MB | 219.5 ms |
| JPEG q85 原尺寸 | 3072×3000 | 680,486 | ~907 KB | 20.2 ms |
| JPEG q85（高 800） | **819×800** | **80,835** | ~108 KB | 缩放 80.2 + 编码 4.7 ms |
| PNG（高 800） | 819×800 | 250,463 | ~334 KB | 缩放 80.2 + 编码 20.8 ms |

**这一行是关键**：3072×3000 缩到高 800 只有 **819×800**（因为虚拟屏是 1.024:1 的近似正方形）。也就是说主屏上的文字要**缩小 3.66 倍**才塞得进常见的 1280×800 预算。能不能看清，属于模型侧，本次【未验证】。

### 2.3 截图的"看不出问题"的陷阱

- 不声明 DPI 时，mss 报告虚拟屏 2524×2040，**仍能成功返回图像，不报错**（见 §3.2）。
- system aware 时，mss 报告 5048×4080，也成功返回图像 —— 但 **61.3% 的像素是黑的**（副屏区域 82.87% 是黑）。不会抛异常，只会让你点到黑边里。

---

## 3. DPI 影响（"看着对点不中"的量化）

脚本：`cua-desktop-bench/dpi_click_test.py`

方法：自建一个纯洋红 `400×300` 无边框窗口 → 用 `PIL.ImageGrab`（已证明 DPI 安全）取物理真相 → 用 mss 取"模型会看到的那张图" → 在图中定位洋红块 → 把**图像坐标**喂给 `SetCursorPos` → 由**另一个 per-monitor V2 子进程**读真实物理光标位置 → 比对。

### 3.1 声明 per-monitor V2

```json
"reported_window_rect": [1200, 800, 1600, 1100],
"physical_bbox_of_window": [1200, 1880, 1600, 2180],   // ImageGrab 以虚拟屏原点为 (0,0)，y+1080
"bbox_in_agent_screenshot": [1200, 1880, 1600, 2180],
"click_target_in_image_coords": [1400, 2030],
"setcursorpos_input": [1400, 950],
"actual_physical_cursor": [1400, 950],
"intended_physical_center": [1400, 950],
"offset_px": [0, 0],
"hit": true
```
**偏移 0 px，命中。** 洋红像素计数 120000 = 400×300，bbox 干净。

### 3.2 不声明 DPI（unaware）

```json
"reported_window_rect": [1200, 800, 1600, 1100],        // 它"以为"的
"physical_bbox_of_window": [2400, 2680, 3072, 3000],     // 真实的（且被屏幕右边缘裁掉）
"magenta_pixels_imagegrab": 215040,                      // = 672×320，说明真实窗口是 800×600 且溢出屏幕
"mss_image_size": [2524, 2040],
"bbox_in_agent_screenshot": null,                        // 截图里根本没有这个窗口
"magenta_pixels_agent_shot": 0
```

四条可量化结论：

1. **窗口置位 2×**：要求 `(1200,800)` 400×300，实际落在 `(2400,1600)` 800×600 → 偏移 `(1200, 800)`，即 100% 误差。
2. **截图是"左上角裁剪"而不是缩放**：把 unaware 的 1536×960 主屏截图，与物理图的两个假设比：
   - 与"整屏缩放到 1536×960"的平均绝对差 = **25.73**
   - 与"左上角 1:1 裁剪"的平均绝对差 = **0.00**
   → 结论：**unaware 进程只拿到左上角 1/4 区域，其余 75% 根本没截到，而且 API 不报错。**
3. **看不见就点不中**：窗口真实位置在物理 `(2400,1600)`，不在那 1/4 区域里，截图中洋红像素 **0 / 120000**。
4. **`SetCursorPos` 与窗口坐标不是同一套**：在 unaware 进程里 `SetCursorPos(600,400)`，用 aware 子进程读回物理坐标仍是 `(600,400)` —— **鼠标 API 不做 DPI 虚拟化，窗口 API 做**。所以混用"窗口坐标"和"截图坐标"时，误差会以 2× 的形式出现。

### 3.3 声明 system aware（混合 DPI 的常见翻车点）

| 指标 | 实测 |
|---|---|
| 虚拟屏画布 | 5048×4080 |
| 画布黑色像素占比 | **61.3%** |
| 副屏区域（请求 3840×2160） | **82.87% 是黑** |
| 与 per-monitor 的 3072×3000 画布黑占比对比 | 13.51%（正常，来自副屏未覆盖区域） |

→ system aware 只在"所有屏幕缩放相同"时成立；这台机器 200% + 100% 混合，直接产出大片黑边，模型会把黑边当成界面。

### 3.4 实验是怎么做的（照此可复现）

> 本节只描述 `dpi_click_test.py` / `analyze_shots.py` / `calibrate_sendinput.py` / `shot.py` 里**真实跑过**的步骤；脚本没留记录的细节一律写"未记录"。

**1）测试窗口怎么建**

- 用 `tkinter` 建窗（`tk.Tk()`），再用 Win32 定位，两者组合是为了"窗口坐标可被我严格控制，又不引入 Qt/Electron 那类额外 DPI 行为"。
- 关键属性（`dpi_click_test.py`）：`overrideredirect(True)` 去掉标题栏与边框（避免标题栏颜色干扰纯色判定）、`attributes("-topmost", True)`（避免被其它窗口遮住导致截图里找不到它）、`configure(bg="#ff00ff")` 把客户区整块填成纯洋红。
- 逻辑尺寸 **400×300**，逻辑坐标 **(1200, 800)**。设置方式：先 `geometry("400x300+1200+800")`，再取 `GetAncestor(winfo_id(), GA_ROOT)` 得到顶层 HWND，调 `SetWindowPos(hwnd, HWND_TOPMOST(-1), 1200, 800, 400, 300, SWP_SHOWWINDOW(0x0040))`，然后 `update() → sleep(0.4) → update()` 等窗口稳定。
- 窗口里**没有任何控件**——它就是一整块纯色矩形。"窗口内控件的位置怎么确定"这个问题在本实验里不存在，因为判定完全不依赖控件：只要能在像素里找到洋红块，就知道窗口真实落在哪。§4 的注入实验才用带按钮/输入框的窗口。
- 选 `(255,0,255)` 的原因：桌面内容里几乎不会出现这个精确值，因此可以按**容差 0（逐像素完全相等）**定位，不会把壁纸近似色算进 bbox。

**2）三种 DPI 模式怎么声明**

- 三种模式都只用同一个 API：`user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(ctx))`，参数分别是 `-1`（DPI_AWARENESS_CONTEXT_UNAWARE）、`-2`（SYSTEM_AWARE）、`-4`（PER_MONITOR_AWARE_V2）。
- **每轮都是新进程**：脚本用 `--mode unaware|system|per_monitor` 选一种，进程启动后**第一件事**就是声明，之后不再改。【API】同一进程的 DPI 感知上下文只允许设置一次，重复调用会返回 FALSE，所以"同进程内切换三种模式做对比"这条路本身不可行——必须靠 `--mode` 起新进程。
- **未使用应用清单（manifest），也未使用旧 API `SetProcessDPIAware()`**。
- 顺序约束：必须先声明，再做任何与窗口/坐标相关的事。`dpi_click_test.py` 里声明在 `import mss` / 建 tk 窗口之前；`shot.py` 同样是先声明再 `import mss` 并抓屏。

**3）怎么测落点、怎么判定命中**

四步，两张图对照：

1. **真相图**：同进程 `PIL.ImageGrab.grab(all_screens=True)`。选它是因为它在三种 DPI 模式下都返回 3072×3000 的物理像素（§2.1 实测），可以当"尺子"用。
2. **模型视角图**：同进程 `mss.mss()` 抓 `sct.monitors[0]`（整幅虚拟屏），`Image.frombytes(... "BGRX")` 转 PIL。
3. **找窗口**：两张图分别做 `ImageChops.difference(img, 纯色图) → convert("L") → point(LUT: 差 ≤ 0 记 255) → getbbox()`，得到洋红 bbox；同时统计洋红像素数，只有 `== 400×300` 且 bbox 宽高等于 400/300 时才认为 bbox 干净（输出里的 `bbox_is_clean`）。
4. **点过去**：取模型视角图里 bbox 的中心 `(cx, cy)`（这是**图像坐标**），按最朴素实现换算成桌面坐标 `dx = monitors[0].left + cx`、`dy = monitors[0].top + cy`，然后 `SetCursorPos(dx, dy)`。

读回用 `GetCursorPos`，但**必须由另一个声明了 per-monitor V2 的子进程去读**（`dpi_click_test.py --read-cursor`）。原因：同进程如果是 unaware，`GetCursorPos` 读到的值也可能被虚拟化，等于用错尺子量错尺子。输出里的 `actual_physical_cursor` 就是子进程读到的值。

**判定命中**：把真相图的 bbox 加上虚拟屏原点 `(SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN)` 换成物理框 `phys_box`，实际光标落在框内即 `hit = true`；`offset_px` = 实际光标 − 该框中心。最后 `SetCursorPos` 还原到实验前坐标并再读一次确认（输出里的 `cursor_restored_to`）。

**4）`0.00` 与 `25.73` 这两个数怎么算出来**

`analyze_shots.py` 做的是"两张图 + 两个假设"的比对：

- 源图：`per_monitor__monitor1.png`，3072×1920，物理像素。
- 待判图：`unaware__monitor1.png`，1536×960，由 unaware 进程截的"主屏"。
- 假设 A「整屏等比缩小」：`full.resize((1536,960), Image.LANCZOS)` —— 如果 mss 是"把整屏缩成一半"，它应该长这样。
- 假设 B「左上角 1:1 裁剪」：`full.crop((0, 0, 1536, 960))` —— 如果 mss 只是"截了左上角那一块"，它应该长这样。
- 差异公式：`ImageChops.difference(待判图, 假设图)` 得逐通道绝对差，`convert("RGB")` 后 `ImageStat.Stat(d).mean` 三个通道再取平均，四舍五入 2 位小数。

结果：假设 A = **25.73**，假设 B = **0.00**。0.00 表示逐像素完全一致 → 待判图就是裁剪，不是缩放。

§3.3 的黑色占比同理：`convert("L") → point(≤8 记 255) → histogram()`，黑像素数 ÷ 总像素数，保留 4 位小数（61.3% / 82.87% / 13.51% 都出自这一步）。

**5）重复次数与稳定性（哪些没记录）**

| 脚本 | 重复 | 说明 |
|---|---|---|
| `dpi_click_test.py` | 每种模式 **1 次有效记录** | **未做重复分布统计**，抖动**未记录**。per_monitor 另有 1 次作废：取色容差用了 10，桌面杂色把 bbox 撑到 `(1200,1880)–(3072,3000)`，洋红像素数与 400×300 校验不符，改回容差 0 后重跑，本文数字取重跑值 |
| `shot.py` | `--iters 5` | 本文唯一有重复采样的实验，报 min/median/max（见 §2.1） |
| `calibrate_sendinput.py` | 6 个坐标点各 1 次 | 无重复；6/6 delta = 0 |
| `input_inject.py` | **1 次有效记录** | 前两次因窗口坐标尚未稳定导致目标算错，已作废；表中命中结果取自最终这次 |

关于"0–3 px"：**它不是统计量**，而是观测值集合——标定脚本 6 个点全部 0 px，窗口内 2 次移动分别为 0 px 和 −3 px（目标 x=320 落到 317，来自 `65535` 归一化取整），合计 8 次样本，**没有做多次重复**。应读作"观测到的上界"，不能当 p95 用。

---

## 4. 输入注入

脚本：`cua-desktop-bench/input_inject.py`（per-monitor V2，自建 tkinter 窗口，`SendInput`）

### 4.1 绝对坐标移动精度

先做独立标定 `calibrate_sendinput.py`：6 个坐标点（含虚拟屏负 y 边界），**全部 delta = 0 px**，`SendInput` 返回 1，`GetLastError=0`。归一化公式：

```
nx = round((x - SM_XVIRTUALSCREEN) * 65535 / (SM_CXVIRTUALSCREEN - 1))
ny = round((y - SM_YVIRTUALSCREEN) * 65535 / (SM_CYVIRTUALSCREEN - 1))
flags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
```

窗口内移动实测：目标 `(120,64)` → 到达 `(120,64)`（delta 0）；目标 `(320,64)` → 到达 `(317,64)`（**delta -3 px，归一化取整误差**）。

### 4.2 命中验证（控件自身回调计数）

| 动作 | 目标 | 结果 |
|---|---|---|
| 左键单击按钮 A | 按钮中心 (120,64) | `A=1, B=0` ✅ 未误触 B |
| 左键单击按钮 B | 按钮中心 (320,64) | `A=1, B=1` ✅ |
| 点击输入框 + 键入 `Hello CUA 中文 123!` | 34 个 Unicode 事件，9.5 ms | 读回 **完全一致** ✅ |
| 右键 | pad 中心 | `<Button-3>` 触发，事件坐标 (240,300) ✅ |
| 双击 | pad 中心，间隔 60 ms | `<Double-Button-1>` 触发 ✅ |
| 滚轮 | delta=120 | `<MouseWheel>` 触发，delta=120 ✅ |
| 中键 | pad 中心 | 未报错 ✅ |
| 光标还原 | 动作前 (2123,-201) | 动作后 **(2123,-201)** ✅ |

### 4.3 键盘功能键：被中文输入法吃掉（本次唯一的真卡点）

现象（脚本 `cua-desktop-bench/key_probe.py`，原始输出 `out/vk_probe.json`）：

| 方式 | SendInput 返回 | 控件收到 |
|---|---|---|
| `KEYEVENTF_UNICODE`（逐字符） | 1 / 1 | `entry = "ab"` ✅ 文字正确插入 |
| `wVk=A` | 1 / 1 | **0 个 KeyPress 事件**，`entry = ""` ❌ |
| `wVk + wScan`（`MapVirtualKey`） | 1 / 1 | 1 个事件，**keycode = 229** ❌ |
| 纯 scan code（`KEYEVENTF_SCANCODE`） | 1 / 1 | **0 个事件** ❌ |
| `wVk=BACK` | 1 / 1 | 1 个事件，**keycode = 229** ❌ |

`229 = 0xE5 = VK_PROCESSKEY`，是 Windows 用来表示"这次按键被 IME 处理了"的哨兵值。本机证据（同一次运行）：

- `GetKeyboardLayout(0) = 0x08040804`（简体中文，微软拼音）
- `ImmGetOpenStatus = 1`（输入法打开）
- 把 IME 上下文从**我们自己的窗口**上摘掉（`ImmAssociateContext(hwnd, NULL)`）后按键仍不到达 —— **推测是 TSF 路径的注入按键被过滤，根因【未验证】**。

**工程后果**：文本输入必须走 `KEYEVENTF_UNICODE`（本机已验证可靠）；`Ctrl+S`、`Alt+Tab`、`Enter`、`Tab` 这类功能键在这台机器上**当前不可用**，除非先让目标应用处于英文输入模式或关掉 IME。这会直接影响桌面 CUA 的可用动作集。

---

## 5. 权限边界

脚本：`cua-desktop-bench/privilege_session.py`

| 项 | 实测 |
|---|---|
| 进程是否提升 | **否**（`TokenElevation = false`） |
| 完整性级别 | **MEDIUM**（RID `0x2000`） |
| 可见顶层窗口 | 33 个，来自 15 个 PID |
| 其中的高权限窗口 | **0 个**（枚举时任务管理器未运行，也没有其它提权程序开窗） |
| 前台窗口 | `Chrome_WidgetWin_1` / `Trae CN.exe` / PID 15628 |

UIPI 探测**做了但没打到靶子**：因为没有高权限窗口，`elevated_windows_probed = []`。

正向对照（证明探测方法本身有效）——同样的 `SendMessageTimeout(WM_NULL)` 探针打同级窗口：

| 进程 | 探针结果 |
|---|---|
| explorer.exe | ok = true, err = 0 |
| Trae CN.exe | ok = true, err = 0 |
| WeChatAppEx.exe | ok = true, err = 0 |
| msedge.exe | ok = true, err = 0 |
| SystemSettings.exe | ok = false, **err = 0**（是超时，不是拒绝） |

→ 判据必须区分两种情况：**`err == 5 (ERROR_ACCESS_DENIED)` 才是 UIPI 拦截**；`ok=false, err=0` 是目标窗口没响应（`SMTO_ABORTIFHUNG` 超时）。用 `ok` 单值判断会把"卡死的应用"误判成"权限不足"。

**结论**：以 MEDIUM 完整性运行，按 UIPI 规则**无法向 HIGH 完整性窗口发送消息/注入输入**；本机实测缺口【未验证】，但规则本身是【API】确定的。实际影响：任务管理器、UAC 提权后的程序、部分安装器，CUA 都点不动。解法只有提升自身权限（会带来副作用）或对这些窗口直接拒绝并报错。

---

## 6. 锁屏 / 会话

| 检测信号 | 本机实测（已解锁） | 锁屏时的预期行为 |
|---|---|---|
| `WTSQuerySessionInformation(WTSConnectState)` | **0 = Active**，SessionId = 1 | 断 RDP / 锁屏后多为 **4 = Disconnected**，锁屏瞬间可能仍是 Active（**未验证**） |
| `WTSClientProtocolType` | **0 = console**（本机物理会话） | RDP 会话为 1 |
| `OpenInputDesktop(...)` | **成功，桌面名 `Default`** | 锁屏时拿不到输入桌面，返回 NULL + `ERROR_ACCESS_DENIED(5)`（【API】，本机无法制造该状态） |
| 截图平均亮度 / 黑像素占比 | 正常桌面 203.42 / 13.5% 黑 | 锁屏画面通常大面积黑，但**不同机器/壁纸差异大，不能当唯一判据**（**未验证**） |
| 前台窗口类名 | `Chrome_WidgetWin_1` | 锁屏时为 `LogonUI` / `LockApp` 类（**未验证**） |

**明确没测的**：锁屏、切换用户、断 RDP、UAC 安全桌面。这些都需要人工干预，我不假装测过。可落地的组合判据建议是 **`OpenInputDesktop` 失败 + `WTSConnectState` 变化**，截图亮度只作为兜底告警。

---

## 7. 动作原语清单（按本次实测能支撑的范围）

| 工具 | 参数 | 返回 | 本机实测状态 |
|---|---|---|---|
| `screenshot` | `monitor: int \| "all"`（0=虚拟屏） | `{png_b64, width, height, origin:[x,y], scale, mean_luma}` | ✅ 66 ms / 3072×3000 |
| `mouse_move` | `x, y`（物理像素，虚拟屏坐标） | `{reached:[x,y], delta:[dx,dy]}` | ✅ 误差 0–3 px |
| `click` | `x, y, button: left\|right\|middle, count: 1\|2` | `{ok, pointer:[x,y]}` | ✅ 三种键、双击均命中 |
| `drag` | `x1,y1,x2,y2, button, steps` | `{ok, path_points}` | ⚠️ 未单独测（可用 down/move/up 组合，原理同上） |
| `type` | `text` | `{ok, chars}` | ✅ 仅 **Unicode 路径**可靠；含中文正常 |
| `key` | `key: "Return"\|"Tab"\|"ctrl+s"` | `{ok}` | ❌ **本机被 IME 拦截，实测不可用** |
| `scroll` | `x, y, dx, dy, amount` | `{ok, delta}` | ✅ 滚轮事件已触发 |
| `wait` | `ms` | `{ok}` | 无风险 |
| `get_focus` | — | `{hwnd, pid, proc, class, title}` | ✅ 枚举可用（33 窗口/15 PID） |
| `cursor_position` | — | `{x, y}` | ✅ 物理坐标，0 误差 |
| `session_state` | — | `{connect_state, protocol, input_desktop_ok}` | ✅ 已解锁态可判 |

统一的坐标约定建议（本次实测推出）：**一律用虚拟屏物理像素，原点 `(SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN)`，动作参数直接吃这个坐标，模型看到的是下采样后的图 + `scale` 字段，回算由运行时做**。不要给模型负坐标，也不要把负坐标归零。

---

## 8. 风险与急停（哪些真能落地）

| 措施 | 能否落地 | 依据 / 实测 |
|---|---|---|
| 动作前记录光标、动作后 `SetCursorPos` 还原 | ✅ 已实测 | 2123,-201 → 动作 → 2123,-201 |
| 动作前校验目标窗口自建/白名单 | ✅ | `get_focus` 可用 |
| 单步后截图哈希比对，无变化即判失败 | ✅ | 截图 66 ms，足够每步一张 |
| 步数上限 + 同点三连击熔断 | ✅ | 纯应用层 |
| 全局热键急停（`RegisterHotKey`，如 `Ctrl+Alt+Esc`） | ⚠️ 需验证 | 热键注册在**本进程**，但输入被 CUA 抢占时热键仍由系统分发，通常可用；**本次未测**，且 IME 可能吃掉部分组合键 |
| 轮询 `GetAsyncKeyState` 作为第二急停通道 | ✅ 可落地 | 比热键更可靠，但需要独立线程轮询 |
| `BlockInput(TRUE)` 冻结用户输入 | ❌ 不建议 | 现代 Windows 上需要更高权限，且一旦崩溃会把用户锁死在无法输入的状态；**未验证**，风险大于收益 |
| 禁止在用户正在输入的窗口上操作 | ⚠️ 部分可落地 | 可用 `GetLastInputInfo` + 前台窗口 PID 判定"用户刚在打字"，但这只是启发式；真正可靠要靠用户显式授权目标窗口 |
| 操作前 `SetForegroundWindow` 抢焦点 | ⚠️ 有副作用 | 会打断用户；且 Windows 有前台锁，非前台进程可能失败 |

**必须承认的一条**：`SendInput` 一旦入队就无法撤回。急停只能保证"下一步不再发"，不能撤销已经发出的那一次点击。所以真正有效的保护是**动作前的白名单校验**，而不是动作后的急停。

---

## 9. 可复现命令

```powershell
$py = "C:\Users\34897\Documents\Trae-CN\temp_20261108_06\cua-desktop-bench\.venv\Scripts\python.exe"
$b  = "C:\Users\34897\Documents\Trae-CN\temp_20261108_06\cua-desktop-bench"

& $py "$b\probe_display.py" --mode per_monitor     # 也测 --mode system / unaware
& $py "$b\shot.py" --mode per_monitor --iters 5
& $py "$b\analyze_shots.py"
& $py "$b\dpi_click_test.py" --mode per_monitor    # 也测 --mode unaware
& $py "$b\calibrate_sendinput.py"
& $py "$b\input_inject.py" --mode per_monitor
& $py "$b\key_probe.py"
& $py "$b\privilege_session.py"
& $py "$b\encode_bench.py"
```

产物：`cua-desktop-bench/out/`（各模式截图、`input_inject_per_monitor.json`、`vk_probe.json`，共 24 个文件 15.67 MB）。
依赖：独立 venv，`mss 10.2.0 / pillow 13.0.0 / pywin32 312 / psutil 7.2.2`，**46.92 MB**，未触碰 feishu 环境。

---

## 10. 直接回答"卡在哪、要多少功夫"

**卡点排序（按是否会阻塞上线）**

1. **DPI 声明（阻塞级，但一次性）**：必须 per-monitor V2 + 应用清单；否则本机会静默丢 75% 画面。这是**代码里的一行 + 一个 manifest**，不是研究问题。
2. **中文输入法吃按键（阻塞级，功能缺失）**：`Enter/Tab/Ctrl+S` 当前不可用。要么接受"只能打字不能按快捷键"，要么在操作前检测 IME 状态并处理。**根因未验证**，这是唯一需要继续投入排查的一项。
3. **分辨率与文字大小（体验级，需模型侧验证）**：3072×3000 → 819×800，文字缩 3.66×。模型能否读清小字，本次没有测（要真调模型）。
4. **多显示器混合缩放（设计级）**：副屏 100%，主屏 200%，负 y。必须按"每屏一套坐标"建模，不能假设单一缩放。
5. **权限（边界级）**：MEDIUM 进程对 HIGH 窗口必然失败，本机无靶子未实测；必须设计"拒绝并上报"而不是静默点击。

**工作量（按交付物计，不给日历时间）**
- 3 个模块：`display`（枚举/DPI/坐标换算）、`capture`（截图+下采样+编码）、`input`（SendInput 封装+白名单+还原）。
- 2 个闸门：① grounding 基准（30 个目标，命中率 ≥90% 才继续）；② 急停与还原的故障注入用例。
- 已可直接复用：本仓库 9 个脚本就是这三个模块的原型，实测数字已经覆盖了它们的正确性边界。

**一句话**：**这台机器上纯视觉桌面 CUA 的"手"是通的（截图 66 ms、点击 0–3 px、打字精确），"眼"取决于 DPI 声明（不声明等于瞎了 3/4），"手指按键"被中文输入法卡住。立项可以，先把 DPI 声明和 IME 两件事做实。**
