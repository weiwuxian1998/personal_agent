# 部署手册（Windows 原生）

> 适用范围：当前仓库 `main` 分支，Windows 10/11 x64 原生部署；不依赖 Docker / WSL。
> 目标：从全新 `git clone` 开始，按本文逐项执行，得到可验证的「只读控制台」或「完整飞书机器人（含网页发送/手机同步）」。
> 整理时间：2026-11-24。若代码有更新，以 `config.py`、`.env.example` 和 `scripts/` 的实际内容为准。

本文是部署的**唯一操作主线**。`README.md` §2 是同一流程的快速版；`docs/OPERATIONS.md` 是原作者机器上的运维记录，其中部分路径和 `DATABASE_URL` 已过时，移植时不要照抄。

---

## 0. 先选部署模式

| 模式 | 需要 | 能得到 | 不需要 |
|---|---|---|---|
| **A. 只读控制台**（最小） | Python、Redis、PostgreSQL、Node（仅构建前端） | 浏览器里观察会话、任务、定时、网关状态 | 飞书应用、LLM Key；网页发送也不可用 |
| **B. 完整飞书机器人** | A 的全部 + 飞书自建应用 + DeepSeek Key | 飞书里收发消息，router/worker 执行任务 | — |
| **C. B + 桌面/浏览器自动化**（可选） | B 的全部 + 本机 Chrome/Chromium | CUA 桌面与浏览器操作 | — |

建议先按 A 跑通，再补 B。A 和 B 共用同一套 Redis / PostgreSQL / `.env`，不需要重复安装。

### 部署完成的最小闭环

- [ ] Python 3.11 虚拟环境可用
- [ ] Redis ≥ 7 可连接
- [ ] PostgreSQL 16 可连接，`assistant` 库存在
- [ ] `.env` 能读出 Redis / PG 配置
- [ ] `ui/web/dist/index.html` 已生成
- [ ] `scripts/gateway.ps1` 与 `scripts/gateway-run.cmd` 的硬编码路径已改成本机路径
- [ ] 控制台 `http://127.0.0.1:8765` 可访问，`redis_ok` / `postgres_ok` 为 `true`
- [ ] （模式 B）gateway 显示 `ESTABLISHED`，飞书发消息能收到回复

---

## 1. 架构与端口

```
飞书 ──长连接──> main_gateway.py（常驻：收消息 / 发回复 / 拉起 router / 定时扫描）
                     │  XADD
                     ▼
              Redis Streams（asst:stream:router / asst:stream:outbox）
                     │  XREADGROUP
                     ▼
              main_router.py（按需启动，空闲 60s 自动退出；LangGraph checkpoint 存 Postgres）
                     │  派生子进程
                     ▼
              worker_runner.py（每个长任务一个进程）
                     │
       ┌─────────────┼─────────────┐
       ▼             ▼             ▼
    tools/*        cua/*         memory/
```

| 组件 | 默认地址 | 用途 |
|---|---|---|
| Web 控制台 `protocol_adapter.py` | `127.0.0.1:8765` | FastAPI + WebSocket；托管 `ui/web/dist`；可观测也可网页发送 |
| Redis | `127.0.0.1:6379` | Stream 总线、去重、心跳、outbox |
| PostgreSQL | `127.0.0.1:5432` | LangGraph checkpoint（`assistant` 库） |
| 飞书开放平台 | 出站长连接 | 事件订阅，不需要公网 IP |

> 控制台**无鉴权**，默认只读；唯一写入口是 `POST /api/messages`（网页发送并镜像到飞书）。只绑定 `127.0.0.1`，不要直接暴露到公网；远程访问请自行加反向代理和认证。

---

## 2. 前置条件

所有命令默认在**仓库根目录**执行。先设置两个变量，后面直接复用：

```powershell
Set-Location 'D:\personal_agent'      # 改成你的实际仓库路径
$Repo = (Resolve-Path .).Path
$Py   = Join-Path $Repo '.venv\Scripts\python.exe'
```

| 组件 | 要求 | 检查命令 | 说明 |
|---|---|---|---|
| Windows | 10/11 x64 | `[Environment]::OSVersion` | 运维脚本和 CUA 仅 Windows |
| Git | 任意较新版本 | `git --version` | 用于拉仓库 |
| Python | **3.11**（实测 3.11.18） | `python --version` | 依赖已钉死；不要用未验证的 3.12/3.13 |
| Node.js | **20 LTS** | `node --version` / `npm.cmd --version` | 仅前端构建需要 |
| Redis | **≥ 7.0 推荐**，硬下限 6.2 | `redis-cli ping` | 代码用 `XAUTOCLAIM`，`XINFO GROUPS` 的 `lag` 需 7.0 |
| PostgreSQL | 16 推荐 | `psql --version` | 只存 checkpoint；库要手工建 |
| 飞书自建应用 | 模式 B 需要 | 开放平台后台 | 长连接模式，无需公网 IP |
| DeepSeek API Key | 模式 B 需要 | — | OpenAI 兼容接口 |

> **前端说明**：`ui/web/dist/` 被 `.gitignore` 排除，**全新 clone 默认没有 `dist`**。因此除非你拿到的是已预构建的发布包，否则 Node 构建是必做步骤，不是可选项。README 里「dist 已存在则跳过」只适用于这种发布包。

---

## 3. 分步部署

### 3.1 Python 环境

确认 `python` 是 3.11；如果不是，用 `py -3.11` 或安装 Python 3.11 后使用绝对路径。

```powershell
python --version
python -m venv .venv
& $Py -m pip install --upgrade pip
& $Py -m pip install -r requirements.txt -r requirements-ui.txt
```

预期：安装无报错。后续所有 Python 命令都必须使用同一个 `.venv\Scripts\python.exe`，不要在不同终端混用 conda / 系统 Python。

如果 `python -m venv` 不可用：

```powershell
py -3.11 -m venv .venv
```

> 依赖版本已冻结，不要自行升级 `requirements.txt` 里的大版本；变更需走 `docs/ENGINEERING_CONTRACT.md` §11。

### 3.2 安装并验证 Redis

三选一，按企业环境选择：

**方式 A（推荐，原生 Windows 构建）**：从 [redis-windows/redis-windows Releases](https://github.com/redis-windows/redis-windows/releases) 下载 `Redis-7.4.x-Windows-x64-msys2-with-Service.zip`（7.x/8.x 均可），解压到**无中文、无空格**的路径，例如 `D:\redis`，然后前台启动：

```powershell
D:\redis\redis-server.exe
```

预期日志出现 `Ready to accept connections`，默认监听 `127.0.0.1:6379`。窗口关闭则 Redis 停止；需要常驻时用包内服务注册脚本注册为 Windows 服务（管理员）。

**方式 B**：复用已有 Redis ≥ 7（包括 WSL 里的实例），确认 `.env` 的 `REDIS_URL` 指向它。

**方式 C**：企业环境不接受社区二进制时，用 Memurai Developer（Redis 7 兼容、原生 Windows 服务）。

验证（`redis-cli` 不在 PATH 时用完整路径，如 `D:\redis\redis-cli.exe`）：

```powershell
redis-cli ping          # 预期 PONG
redis-cli info server   # 确认 redis_version >= 7
```

> 不要使用微软旧版 Redis 3.2 或 tporadowski 5.0：会报 `unknown command XAUTOCLAIM`，或 `lag` 恒为 `None`。

### 3.3 安装并初始化 PostgreSQL

```powershell
winget install --id PostgreSQL.PostgreSQL.16 -e --accept-package-agreements --accept-source-agreements
```

安装向导中设置 `postgres` 超级用户密码，端口保持 `5432`。装完后 PostgreSQL 通常已注册为 Windows 服务并开机自启。

创建业务库（只需一次；提示已存在就跳过）：

```powershell
& "C:\Program Files\PostgreSQL\16\bin\psql.exe" -U postgres -c "CREATE DATABASE assistant;"
```

验证：

```powershell
& "C:\Program Files\PostgreSQL\16\bin\psql.exe" "postgresql://postgres:你的密码@127.0.0.1:5432/assistant" -c "SELECT 1;"
```

预期返回一行 `1`。

> 密码里若含 `@`、`:`、`/` 等字符，写进 DSN 前要先做 URL 编码（例如 `@` → `%40`），否则会被当成连接串分隔符。

### 3.4 配置 `.env`

```powershell
Copy-Item .env.example .env
notepad .env
```

按部署模式填写：

| 变量 | A. 只读控制台 | B. 完整机器人 | 示例 / 说明 |
|---|---|---|---|
| `REDIS_URL` | 必填 | 必填 | `redis://127.0.0.1:6379/0` |
| `POSTGRES_DSN` | 必填 | 必填 | `postgresql://postgres:密码@127.0.0.1:5432/assistant` |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 不需要 | 必填 | 飞书开放平台 → 应用凭证 |
| `LLM_API_KEY` | 不需要 | 必填 | DeepSeek Key；`LLM_BASE_URL` / `LLM_MODEL` 默认即可 |
| `LLM_SUPPORTS_VISION` | 可选 | 可选 | 文本模型保持 `false`；换视觉模型时改 `true` |
| `DEEPSEEK_SEARCH_API_KEY` | 不需要 | 可选 | WebSearch/WebFetch 专用，和 LLM Key 物理隔离；不填则返回 `not_configured` |
| `DATA_DIR` | 可选 | 可选 | 运行数据根目录，默认 `%USERPROFILE%\.assistant` |
| `WORKSPACE_ROOT` | 可选 | 可选 | 助手可写工作区，默认 `%USERPROFILE%\assistant-workspace` |

**注意 `.env.example` 的默认值**：`LLM_SUPPORTS_VISION=false` 是给 `deepseek-flash` 这类文本模型的；`config.py` 的类默认值是 `true`，但 `.env` 里的显式值会覆盖它。部署时以 `.env` 为准，不要改 `config.py`。

验证配置能被正确读取：

```powershell
& $Py -c "import config; s=config.settings; print('redis=', s.redis_url); print('pg=', bool(s.postgres_dsn)); print('feishu=', bool(s.feishu_app_id and s.feishu_app_secret)); print('llm=', bool(s.llm_api_key))"
```

预期：打印 Redis URL、`pg= True`；模式 B 下 `feishu= True`、`llm= True`。若抛异常，先检查 `.env` 格式（不要加多余引号）和变量名。

**密钥红线**：`.env` 已在 `.gitignore` 中排除，任何真实密钥都不要提交。凭证也可以放在仓库外，用 `ASSISTANT_ENV_FILE` 指向该文件：

```powershell
$env:ASSISTANT_ENV_FILE = 'D:\secrets\personal-agent.env'
```

> 不要把 `LLM_API_KEY` 填到 `DEEPSEEK_SEARCH_API_KEY`，也不要用搜索 Key 调 LLM；代码按契约禁止两边互相 fallback。

### 3.5 构建前端（全新 clone 必做）

先确认 `ui\web\dist\index.html` 是否存在：

```powershell
Test-Path ui\web\dist\index.html
```

返回 `False` 时执行以下命令。顺序不能反：`ui/web` 依赖 `ui/sdk` 的构建产物。

```powershell
node --version          # 预期 v20.x
npm.cmd --prefix ui/sdk install
npm.cmd --prefix ui/sdk run build
npm.cmd --prefix ui/web install
npm.cmd --prefix ui/web run build
```

预期：

- `ui\sdk\dist\index.js` 存在
- `ui\web\dist\index.html` 存在
- `ui/web` 的 `tsc --noEmit` 无类型错误

> 若 PowerShell 报 `npm.ps1 cannot be loaded because running scripts is disabled`，改用 `npm.cmd`（上面已经这样写），或在当前窗口临时执行 `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`。不要为此改全局执行策略。

### 3.6 修正 gateway 脚本里的硬编码路径（关键）

当前仓库的 `scripts/gateway.ps1` 和 `scripts/gateway-run.cmd` 保留了原作者机器路径。**两个文件都要改**，只改一个会导致启动失败或状态检测不到。

`scripts\gateway.ps1` 顶部两行：

```powershell
$ProjectDir = 'D:\personal_agent'
$PythonExe  = 'D:\personal_agent\.venv\Scripts\python.exe'
```

改成你的实际值：

| 变量 | 含义 | 本机示例 |
|---|---|---|
| `$ProjectDir` | 仓库根目录绝对路径 | `D:\personal_agent` |
| `$PythonExe` | 3.1 创建的虚拟环境解释器绝对路径 | `D:\personal_agent\.venv\Scripts\python.exe` |

`scripts\gateway-run.cmd` 里的三处路径也要同步：

```bat
cd /d "D:\personal_agent"
"D:\personal_agent\.venv\Scripts\python.exe" -u main_gateway.py 1>> "D:\personal_agent\logs\gateway-live.log" 2>&1
```

对应关系：

| 位置 | 改成 |
|---|---|
| `cd /d "..."` | 仓库根目录 |
| 行首的 Python 路径 | `$PythonExe` 的同一个路径 |
| `1>> "..."` 的日志路径 | `<仓库根目录>\logs\gateway-live.log` |

> 为什么两个都要改：`gateway.ps1` 通过 WMI 拉起 `gateway-run.cmd`，真正执行 `main_gateway.py` 的是后者；如果后者仍 `cd` 到旧目录或调用旧 Python，`gateway.ps1` 会报「process did not appear within 30s」，或者进程出现但状态检测不到。

改完后用绝对路径确认两个文件都指向真实存在的解释器：

```powershell
Test-Path 'D:\personal_agent\.venv\Scripts\python.exe'
Select-String -Path scripts\gateway.ps1 -Pattern 'ProjectDir|PythonExe'
Select-String -Path scripts\gateway-run.cmd -Pattern 'cd /d|python.exe|gateway-live'
```

> 不需要管理员权限。`gateway.ps1 start` 通过 WMI 的 `Win32_Process.Create` 创建进程，不依赖当前终端；关闭启动窗口不会杀掉 gateway。不要把它注册成 Windows 服务：CUA 需要交互式桌面。

### 3.7 启动 Web 控制台

在**单独一个终端**中启动，保持窗口打开：

```powershell
& $Py protocol_adapter.py
```

默认监听 `127.0.0.1:8765`。可用参数：

```powershell
& $Py protocol_adapter.py --host 127.0.0.1 --port 8766
```

验证：

```powershell
curl.exe -s http://127.0.0.1:8765/api/status
& $Py -c "import requests; print(requests.post('http://127.0.0.1:8765/api/messages', json={'text': ''}).status_code)"
```

预期：

- `/api/status` 返回 JSON，`redis_ok: true`、`postgres_ok: true`；`gateway.alive: false` 属正常（还没启动机器人）。
- `POST /api/messages` 空消息返回 `400`（端点已启用；发送需要 gateway 在线且有飞书会话）。
- 浏览器打开 <http://127.0.0.1:8765> 能看到控制台（总览 / Router 上下文 / Workers / 定时）。
- 全新部署各页为空态，属正常。
- 若 `/` 只返回 JSON 占位说明，说明 `ui/web/dist` 没构建，回到 3.5。

**网页发送与手机同步**：总览页对话框底部有输入框。发送后：

1. 后端调用飞书 API，把 `【网页】+ 原话` 作为机器人消息发到当前会话 —— 手机端立刻可见；
2. 同时把同一条内容以 `user_message` 写入 router stream，router 正常决策；
3. 助理回复继续走 outbox → gateway → 飞书，网页和手机都能看到。

网页消息没有飞书 `om_` message_id，因此回复不引用原始消息，而是直接发到同一 chat_id。

助理回复中提到的本地文件、`products` 和飞书附件会渲染成文件卡片；点击后在弹层里预览图片、PDF、音视频或文本。Markdown 会渲染成排版后的正文，HTML 会在 sandbox iframe 中渲染（两者都经 DOMPurify 清洗）。文件由 `GET /api/files` 提供，仅允许 `workspace_root`、`data_dir/inbox`、`data_dir/tool_outputs`、`data_dir/workers` 内路径，越界返回 403。

### 3.8 配置并启动飞书机器人（模式 B）

**飞书开放平台**：

1. 创建**自建应用**，启用**机器人**能力。
2. 事件订阅选择**长连接**模式（无需公网 IP），订阅 `im.message.receive_v1`。
3. 按需要开通消息相关权限：接收消息、发送文本/图片/文件、引用回复等。权限名称以开放平台当前界面为准。
4. 把 App ID / App Secret 填入 `.env` 的 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`。

**启动 gateway**：

```powershell
scripts\start-gateway.cmd
```

等价命令：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "$Repo\scripts\gateway.ps1" -Action start
```

预期：

- 首次启动要 import `lark_oapi`，约 50–60 秒；脚本最多等 150 秒。
- 日志出现 `connected to wss` 后，脚本打印类似 `READY: ... connected to wws` 的行。
- 看到 `READY` 后可以关闭启动窗口，gateway 进程已经脱离终端。

首次启动前确保 Redis / PostgreSQL 都在运行。启动后不要在另一个窗口再启动一次：`start` 检测到已有 `main_gateway.py` 进程时会拒绝启动（退出码 2）。飞书把同一事件只投给同一应用的一条长连接，多实例会导致「有时回有时不回」。

### 3.9 可选：CUA 桌面/浏览器自动化

只有需要助手操控本机桌面或浏览器时才配置。桌面任务不需要浏览器；浏览器任务需要一个本机 Chrome/Chromium 可执行文件。

在 `.env` 中追加（或修改）：

```dotenv
CUA_BROWSER_EXE=D:\chrome-win64\chrome.exe
CUA_BROWSER_AUTOSTART=false
```

验证路径存在：

```powershell
Test-Path 'D:\chrome-win64\chrome.exe'
```

注意：

- `config.py` 里默认值是原作者机器的 `D:\repo\chrome-win64\chrome.exe`，移植后通常不存在；必须用 `.env` 覆盖，不要改 `config.py`。
- `CUA_BROWSER_AUTOSTART=true` 会在任务开始前自动拉起内核；桌面类任务保持 `false` 即可。
- CUA 依赖交互式 Windows 桌面，必须在当前登录用户会话里运行，不要作为 Windows 服务运行。
- 首次使用前确认助手有权限访问屏幕和输入；远程桌面断开后，桌面自动化可能失效。

---

## 4. 验收清单

### 4.1 模式 A：只读控制台

| # | 检查 | 命令 | 通过标准 |
|---|---|---|---|
| 1 | Redis 版本 | `redis-cli info server` | `redis_version` ≥ 7（至少 ≥ 6.2） |
| 2 | Redis 连通 | `redis-cli ping` | `PONG` |
| 3 | PostgreSQL 连通 | `psql "<POSTGRES_DSN>" -c "SELECT 1;"` | 返回 `1` |
| 4 | 配置可读 | `& $Py -c "import config; ..."` | 打印 URL 与 `True`，无异常 |
| 5 | 前端产物 | `Test-Path ui\web\dist\index.html` | `True` |
| 6 | 控制台存活 | `curl.exe -s http://127.0.0.1:8765/api/status` | HTTP 200，`redis_ok` / `postgres_ok` 均 `true` |
| 7 | 发送端点存在 | `& $Py -c "import requests; print(requests.post('http://127.0.0.1:8765/api/messages', json={'text': ''}).status_code)"` | 输出 `400`（空消息被拒） |
| 8 | 页面 | 浏览器打开 `http://127.0.0.1:8765` | 渲染控制台，无 JS 报错 |

### 4.2 模式 B：完整机器人

在 4.1 全通过后，再验收：

| # | 检查 | 命令 | 通过标准 |
|---|---|---|---|
| 9 | gateway 进程 | `scripts\status-gateway.cmd` | 显示 `Process : 1 instance(s)`，`Connection : ESTABLISHED` |
| 10 | 长连接日志 | `Select-String logs\gateway-live.log -Pattern 'connected to wss'` | 有匹配行 |
| 11 | 端到端（飞书） | 飞书给机器人发一条消息 | 收到回复；`/api/status` 中 outbox 无持续积压 |
| 12 | 端到端（网页） | 在控制台总览页发送一条消息 | 网页显示用户消息；手机飞书收到 `【网页】` 镜像；随后收到助理回复 |
| 13 | 文件预览 | 在控制台对话里点击文件卡片 | 文本/图片/PDF 弹层预览正常；`GET /api/files` 越界路径返回 403 |

> `scripts/check_adapter.py` 是**开发验收脚本**，断言要求已有历史会话数据（`sessions.total > 0`），**不适合空库的全新部署**。全新部署用 4.1 的 1–8 项。

---

## 5. 日常运维

### 5.1 gateway 启停

| 操作 | 双击文件 | 等价命令 |
|---|---|---|
| 启动 | `scripts\start-gateway.cmd` | `powershell -NoProfile -ExecutionPolicy Bypass -File "$Repo\scripts\gateway.ps1" -Action start` |
| 停止 | `scripts\stop-gateway.cmd` | `... -Action stop` |
| 状态 | `scripts\status-gateway.cmd` | `... -Action status` |
| 重启 | — | `... -Action restart` |
| 看日志 | — | `... -Action logs -Tail 80` |
| 实时跟日志 | — | `... -Action logs -Tail 20 -Follow` |
| 手动轮转日志 | — | `... -Action rotate` |

`gateway.ps1` 的 `-Action` 合法值：`start` / `stop` / `restart` / `status` / `logs` / `rotate`；不传 `-Action` 时默认 `status`。`-Tail` 默认 40，只对 `logs` 生效。

### 5.2 运行期目录

| 内容 | 默认位置 |
|---|---|
| gateway 日志 | `<仓库>\logs\gateway-live.log` |
| gateway pid 文件 | `<仓库>\logs\gateway.pid`（start 写入，stop 删除） |
| router 日志 | `%USERPROFILE%\.assistant\logs\router-*.log` |
| worker 日志 | `%USERPROFILE%\.assistant\logs\worker-<task_id>.log` |
| SQLite | `%USERPROFILE%\.assistant\db\assistant.db` |
| worker 任务目录 | `%USERPROFILE%\.assistant\workers\` |
| 飞书附件收件箱 | `%USERPROFILE%\.assistant\inbox\` |
| 助手可写工作区 | `%USERPROFILE%\assistant-workspace` |

改 `DATA_DIR` / `WORKSPACE_ROOT` 时，两处路径都要是绝对路径或可展开的 `%VAR%` / `~` 路径；`config.py` 会展开，不要写相对路径。

### 5.3 行为约定

- **不做开机自启**：重启机器后手动 `start-gateway.cmd`；Redis / PostgreSQL 若注册为服务会自动启动。
- **只跑一个 gateway**：多实例会分流飞书事件，表现为随机丢消息。
- **router 空闲退出是正常的**：默认 60 秒无事件自动退出，有新消息时 gateway 再拉起；控制台显示「待机」黄条不一定是故障。
- **停止只杀匹配进程**：`gateway.ps1` 只结束「Python 路径等于 `$PythonExe` 且命令行含 `main_gateway.py`」的进程，不会误杀其它 Python。
- **日志为 UTF-8**：启动包装脚本设置了 `PYTHONUTF8=1` / `PYTHONIOENCODING=utf-8`。

---

## 6. 故障排查

按「先进程、再连接、后配置」的顺序排查，不要跳步。

| 现象 | 优先检查 | 处理 |
|---|---|---|
| `start` 报 `Refusing to start: N gateway instance(s) already running` | 已有实例 | 先 `stop-gateway.cmd`，确认无残留进程，再启动 |
| `start` 报 `Failed: gateway process did not appear within 30s` | `scripts\gateway-run.cmd` 的 `cd /d` 和 Python 路径；`$PythonExe` | 按 3.6 同步两个文件；看 `logs\gateway-live.log` 末尾 |
| 进程存在但 `status` 显示 `NOT RUNNING` | `$PythonExe` 与实际解释器路径是否完全一致 | 用 `Resolve-Path` 得到真实路径后改 `gateway.ps1` |
| 150 秒内没有 `READY` | 飞书凭证、网络、Redis/PG、日志 | 看 `logs\gateway-live.log`；确认 `connected to wss` 是否出现 |
| `unknown command XAUTOCLAIM` 或 `lag` 恒为 `None` | Redis 版本 | 换 7.x/8.x；不要用 3.2/5.0 旧移植版 |
| `/api/status` 中 `redis_ok:false` | Redis 未启动或 `REDIS_URL` 错 | 先 `redis-cli ping`，再核对 `.env` |
| `/api/status` 中 `postgres_ok:false` | DSN 错 / 服务未起 / 库不存在 | 按 3.3 复检；表会自动建，库要手工建 |
| 打开 8765 只有 JSON 没有页面 | `ui/web/dist` 不存在 | 按 3.5 构建；确认构建没有报 TypeScript 错 |
| `npm.ps1 cannot be loaded...` | PowerShell 执行策略 | 改用 `npm.cmd`，或当前窗口临时 `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` |
| 端口被占 | 8765/6379/5432 冲突 | 控制台用 `--port 8766`；`netstat -ano \| findstr :端口` |
| 飞书消息有时回有时不回 | 是否跑了多个 gateway | `stop-gateway.cmd` 后只启动一个 |
| 收到事件但不回复 | router 消费失败 / 模型报错 | 看 `.assistant\logs\router-*.log` 与 `worker-*.log`；lag 见 `docs/OPERATIONS.md` §4 |
| WebSearch/WebFetch 返回 `not_configured` | `DEEPSEEK_SEARCH_API_KEY` 未填 | 填搜索专用 Key；不要复用 LLM Key |
| 重启机器后机器人不工作 | 本方案不做开机自启 | 手动 `start-gateway.cmd` |
| CUA 报浏览器不存在 | `CUA_BROWSER_EXE` 路径 | 设置真实 Chrome/Chromium 路径，`Test-Path` 确认 |

### 日志关键字

| 关键字 | 含义 |
|---|---|
| `收到原始事件` | 飞书事件到达 gateway |
| `入队` | 写入 router stream |
| `已拉起 router` / `router 正常退出（空闲）` | router 生命周期 |
| `connected to wss` | 飞书长连接建立 |
| `发送成功 ... duration_ms=` | outbox 发送完成 |

---

## 7. 安全与变更边界

- 控制台**默认只读、无鉴权**：`POST /api/messages` 可写并会向飞书发送消息；保持 `127.0.0.1` 绑定，需要远程访问时自行加反向代理、TLS 和认证。
- Redis / PostgreSQL 只监听本机或内网，不要暴露公网；生产环境给 PG 设置强密码。
- 密钥只放 `.env` 或 `ASSISTANT_ENV_FILE` 指向的仓库外文件；日志会对 Key 脱敏，但不要把 Key 贴进聊天或提交到 git。
- `LLM_API_KEY` 与 `DEEPSEEK_SEARCH_API_KEY` **物理隔离**：前者只给 LLM，后者只给 `/v1/tools/search` 与 `/v1/tools/fetch`，代码不允许互相 fallback。
- 以下为**冻结契约文件**，部署时不要改；需要变更走 `docs/ENGINEERING_CONTRACT.md` §11：
  `contracts.py`、`config.py`、`bus/envelope.py`、`channel/base.py`、`agents/context.py`、`agents/vision.py`、`requirements.txt`、`.env.example`、`.gitignore`、`docs/ENGINEERING_CONTRACT.md`。
- SQLite 业务表只由 router 写；控制台以只读模式打开。不要给 Redis 增加新的状态键。
- CUA 能操控本机桌面和浏览器，只对可信任务开启；不要把 gateway 注册成 Windows 服务后指望 CUA 可用。

---

## 8. 相关文档

| 文档 | 用途 |
|---|---|
| [`README.md`](../README.md) | 项目概览、快速部署、目录结构 |
| [`docs/OPERATIONS.md`](OPERATIONS.md) | 原作者机器上的 gateway 运维记录；路径和 `DATABASE_URL` 已过时，移植时以本文为准 |
| [`docs/COMMANDS.md`](COMMANDS.md) | Redis/进程/日志排查命令速查；其中 `$PY` 仍是原作者路径，执行前改成自己的 |
| [`docs/ENGINEERING_CONTRACT.md`](ENGINEERING_CONTRACT.md) | 工程契约、冻结文件、变更流程 |
| [`docs/MEMORY_DESIGN.md`](MEMORY_DESIGN.md) | 记忆层目录布局与设计 |
| [`docs/CUA_UIA_GUIDE.md`](CUA_UIA_GUIDE.md) | Windows 桌面自动化实现指南 |
| [`docs/CUA_BROWSER_RESEARCH.md`](CUA_BROWSER_RESEARCH.md) | 浏览器操控调研 |

---

## 9. 最小命令序列（熟练者速查）

```powershell
Set-Location 'D:\personal_agent'
$Repo = (Resolve-Path .).Path
$Py   = Join-Path $Repo '.venv\Scripts\python.exe'

python -m venv .venv
& $Py -m pip install --upgrade pip
& $Py -m pip install -r requirements.txt -r requirements-ui.txt

# Redis / PostgreSQL 按 3.2 / 3.3 安装并建库
Copy-Item .env.example .env
notepad .env

npm.cmd --prefix ui/sdk install
npm.cmd --prefix ui/sdk run build
npm.cmd --prefix ui/web install
npm.cmd --prefix ui/web run build

# 先改 scripts\gateway.ps1 和 scripts\gateway-run.cmd（见 3.6）
& $Py protocol_adapter.py
# 浏览器打开 http://127.0.0.1:8765 验收

scripts\start-gateway.cmd
```
