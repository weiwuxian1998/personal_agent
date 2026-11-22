# personal-assistant

飞书个人助理：常驻 gateway 接收飞书消息，按需拉起 router（LangGraph + Postgres checkpoint）决策，长任务交给 worker 子进程执行；Redis Streams 做事件总线。附带一个**只读 Web 控制台**，在浏览器里观察会话、任务、定时、成本与网关状态。

本文档面向**部署者（人或自动化智能体）**，以 Windows 原生部署为主线，不依赖 Docker / WSL。每一步都给出命令与预期结果，按顺序执行即可。

```
飞书 ──长连接──> main_gateway.py（常驻：收消息 / 发回复 / 拉起 router / 定时扫描 / 看门狗）
                     │  XADD
                     ▼
              Redis Streams（asst:stream:router / asst:stream:outbox）
                     │  XREADGROUP
                     ▼
              main_router.py（按需启动，空闲 60s 退出；LangGraph checkpoint 存 Postgres）
                     │  派生子进程
                     ▼
              worker_runner.py（每任务一个，长命令 / 浏览器 / 文件操作）

protocol_adapter.py（只读控制台后端，FastAPI + WebSocket，默认 127.0.0.1:8765）
   └── 托管 ui/web/dist 前端；只读 Redis / SQLite / Postgres，无任何写命令
```

---

## 1. 环境要求

| 组件 | 版本要求 | 说明 |
|---|---|---|
| OS | Windows 10/11 x64（主线）；Linux/macOS 亦可 | 运维脚本 `scripts/gateway.ps1` 目前仅 Windows |
| Python | **3.11**（实测 3.11.18） | 依赖版本已钉死，勿用未验证的新版本 |
| Redis | **≥ 7.0 推荐**（硬下限 6.2） | 代码使用 `XAUTOCLAIM`（6.2+）与 `XINFO GROUPS` 的 `lag` 字段（7.0+）。**微软旧版 3.2、tporadowski 5.0 均不可用** |
| PostgreSQL | 16 推荐 | 只存 LangGraph checkpoint；表首次启动自动创建（幂等迁移） |
| Node.js | 20 LTS，**可选** | 仅当 `ui/web/dist/index.html` 不存在时需要构建前端 |
| 飞书自建应用 | 可选 | 只跑只读控制台不需要；完整机器人需要（长连接模式，无需公网 IP） |
| DeepSeek API Key | 完整机器人需要 | OpenAI 兼容接口；控制台本身不调用 LLM |

## 2. 快速部署（Windows）

以下命令均在**仓库根目录**执行。PowerShell 与 cmd 均可，注意引号差异。

### 2.1 安装 PostgreSQL 并建库

```powershell
winget install --id PostgreSQL.PostgreSQL.16 -e
```

安装向导中设置 `postgres` 超级用户密码（记住它），端口保持默认 5432。装完自带 Windows 服务、开机自启。然后建一个空库：

```powershell
& "C:\Program Files\PostgreSQL\16\bin\psql.exe" -U postgres -c "CREATE DATABASE assistant;"
```

**预期**：输出 `CREATE DATABASE`。checkpoint 表无需手工建，router 首次启动时自动创建。

> 已有 Postgres 实例可跳过安装，直接建库。

### 2.2 安装 Redis（三选一）

**方式 A（推荐，原生绿色版）**：从社区构建 [redis-windows/redis-windows Releases](https://github.com/redis-windows/redis-windows/releases) 下载 `Redis-7.4.x-Windows-x64-msys2-with-Service.zip`（或 8.x；msys2 构建是原生 Windows 程序，非官方出品，本地开发足够）。解压到无中文、无空格路径（如 `D:\redis`），前台启动：

```powershell
D:\redis\redis-server.exe
```

**预期**：日志出现 `Ready to accept connections`，默认监听 `127.0.0.1:6379`。窗口关闭则 Redis 停止；包内附服务注册脚本，需要常驻可自行注册为 Windows 服务（管理员）。

**方式 B**：机器上已有 Redis ≥ 7（含 WSL 内的实例），直接复用，确认 `REDIS_URL` 指向它即可。

**方式 C（企业环境不接受社区二进制时）**：Memurai Developer（免费、Redis 7 兼容、原生 Windows 服务）。

验证（任一方式）：

```powershell
redis-cli ping        # 预期 PONG
redis-cli info server # 确认 redis_version ≥ 7
```

### 2.3 Python 依赖

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-ui.txt
```

**预期**：安装成功无报错。后续所有 `python` 命令都请用同一个解释器（`.venv\Scripts\python.exe`，或你自己的 conda env）。

### 2.4 配置 `.env`

```powershell
copy .env.example .env
```

编辑 `.env`，最小可用配置：

| 变量 | 只读控制台 | 完整机器人 | 示例 / 说明 |
|---|---|---|---|
| `REDIS_URL` | 必填 | 必填 | `redis://127.0.0.1:6379/0` |
| `POSTGRES_DSN` | 必填 | 必填 | `postgresql://postgres:你的密码@127.0.0.1:5432/assistant` |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 不需要 | 必填 | 飞书开放平台 → 应用凭证 |
| `LLM_API_KEY` | 不需要 | 必填 | DeepSeek key；`LLM_BASE_URL` / `LLM_MODEL` 默认即可 |
| `DEEPSEEK_SEARCH_API_KEY` | 不需要 | 可选 | 不填则 WebSearch/WebFetch 返回 `not_configured`，不影响其它功能 |
| `DATA_DIR` | 可选 | 可选 | 运行数据根目录，默认 `%USERPROFILE%\.assistant`（SQLite、日志、收件箱） |
| `WORKSPACE_ROOT` | 可选 | 可选 | 助手可写工作区，默认 `%USERPROFILE%\assistant-workspace` |

其余字段保持 `.env.example` 默认值即可。凭证也可放仓库外：设环境变量 `ASSISTANT_ENV_FILE` 指向该文件。

**安全红线**：`.env` 及任何含真实密钥的文件**绝不提交 git**（`.gitignore` 已排除 `.env`；注意它不排除 `.env.bak-*` 之类的备份名，不要创建这类备份后 `git add .`）。

### 2.5 前端（dist 已存在则跳过）

检查 `ui\web\dist\index.html`：

- **存在** → 跳过本节，控制台直接可用（无需 Node）。
- **不存在** → 需要 Node 20+，按顺序构建（web 依赖 sdk 的构建产物，顺序不能反）：

```powershell
npm --prefix ui/sdk install
npm --prefix ui/sdk run build
npm --prefix ui/web install
npm --prefix ui/web run build
```

**预期**：`ui/web/dist/index.html` 生成，`tsc --noEmit` 无类型错误。

### 2.6 启动只读控制台

```powershell
python protocol_adapter.py
```

打开 <http://127.0.0.1:8765>。可选参数：`--host`（默认 127.0.0.1）、`--port`（默认 8765）。

**预期**：

- `GET /api/status` 返回 JSON，`redis_ok: true`、`postgres_ok: true`；`gateway.alive: false` 属正常（还没启动机器人）。
- `GET /` 返回控制台页面（总览 / Router 上下文 / Workers / 定时 / 成本五个页签）。若 dist 未构建，返回 JSON 占位说明。
- 全新部署没有历史数据时各页显示空态，属正常。

控制台是**纯只读**：不发送消息、不写 Redis、SQLite 以 `mode=ro` 打开；且**无鉴权**，请保持绑定 127.0.0.1，不要暴露公网。

### 2.7 （可选）完整飞书机器人

1. 飞书开放平台：创建自建应用 → 开启**机器人**能力 → 事件订阅选择**长连接**模式（无需公网 IP），订阅 `im.message.receive_v1`；按功能开通 im 相关发送权限。凭证填入 `.env`。
2. 修改 `scripts\gateway.ps1` 顶部两个变量为你的实际路径：`$ProjectDir`（仓库根目录）、`$PythonExe`（2.3 的解释器绝对路径）。**脚本目前硬编码了原作者机器路径，不改必失败。**
3. 双击 `scripts\start-gateway.cmd`（等价 `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\gateway.ps1 -Action start`）。

**预期**：首次启动 import 约 50–60 秒，日志出现 `connected to wss` 后脚本打印 `READY`，此时可关闭启动窗口（进程经 WMI 脱管，不随终端退出）。

日常运维：`status-gateway.cmd` / `stop-gateway.cmd`；日志 `logs\gateway-live.log`。**严禁同时跑两个 gateway**（飞书长连接对同应用单播分流，多实例表现为随机丢消息）；启动脚本自带单实例检查。详细排查见 `docs/OPERATIONS.md`。

## 3. 部署验收清单（给自动化智能体）

按序执行，全部满足才算部署成功：

| # | 检查 | 命令 | 通过标准 |
|---|---|---|---|
| 1 | Redis 版本 | `redis-cli info server` | `redis_version` ≥ 7（至少 ≥ 6.2） |
| 2 | Redis 连通 | `redis-cli ping` | `PONG` |
| 3 | Postgres 连通 | `psql "<POSTGRES_DSN>" -c "SELECT 1;"` | 返回 1 |
| 4 | 配置可读 | `python -c "import config; s=config.settings; print(s.redis_url, bool(s.postgres_dsn))"` | 打印 URL 与 `True`，无异常 |
| 5 | 前端产物 | 检查 `ui/web/dist/index.html` | 存在（或已按 2.5 构建） |
| 6 | 控制台存活 | `curl http://127.0.0.1:8765/api/status` | HTTP 200，`redis_ok`/`postgres_ok` 均 true |
| 7 | 只读约束 | `curl -X POST http://127.0.0.1:8765/api/messages` | HTTP 404/405（写路径已移除） |
| 8 | 页面 | 浏览器打开 `http://127.0.0.1:8765` | 渲染控制台，无 JS 报错 |
| 9 | （完整部署）gateway | `scripts\status-gateway.cmd` | 进程存在、日志有 `connected to wss` |
| 10 | （完整部署）端到端 | 飞书给机器人发一条消息 | 收到回复；`/api/status` 中 outbox 无持续积压 |

注意：`scripts/check_adapter.py` 是**开发验收脚本**，断言里要求已有历史会话数据（`sessions.total > 0`），**不适合空库的全新部署**；全新部署用上表 1–8 验收。

## 4. 目录结构

```
main_gateway.py        常驻进程：飞书长连接 + outbox 发送 + router 拉起 + 定时/看门狗
main_router.py         按需决策进程（由 gateway 拉起，勿手动常驻）
worker_runner.py       长任务子进程（由 router 派生）
protocol_adapter.py    只读 Web 控制台后端（FastAPI + WS，自带 __main__）
config.py              全部配置项（只读 .env / 环境变量）★冻结
contracts.py           事件/数据契约 ★冻结
bus/                   Redis Streams 封装 ★envelope.py 冻结
channel/               飞书 / console 渠道 ★base.py 冻结
agents/                LangGraph 装配、上下文、视觉 ★context.py/vision.py 冻结
store/                 SQLite（业务表，仅 router 写）+ Postgres checkpointer
tools/                 助手工具集（Shell/文件/Web/CUA 等）
ui/sdk/                TS 客户端 SDK + asst-cli（REST/WS 协议封装）
ui/web/                React 控制台（Vite；dist 为构建产物）
scripts/               gateway 运维脚本（Windows）+ 各模块验收脚本
docs/                  工程契约 / 运维手册 / 排查命令
```

运行期数据不在仓库内：`DATA_DIR`（默认 `~/.assistant`：SQLite `db/assistant.db`、日志、inbox、workers）与 `WORKSPACE_ROOT`（助手唯一可写区）。

## 5. 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| router/gateway 报 `unknown command XAUTOCLAIM` 或 lag 恒为 None | Redis 版本过老（3.2/5.0 旧移植版） | 换 2.2 的 7.x/8.x 构建 |
| `/api/status` 里 `redis_ok:false` | Redis 未启动或 `REDIS_URL` 错 | 先 `redis-cli ping` |
| `postgres_ok:false` | DSN 错 / 服务未起 / 库不存在 | 按 2.1 复检；表会自动建，库要手工建 |
| 打开 8765 只有 JSON 没有页面 | `ui/web/dist` 不存在 | 按 2.5 构建，或确认拿到的是含 dist 的版本 |
| 端口被占 | 8765/6379/5432 冲突 | `python protocol_adapter.py --port 8766`；`netstat -ano \| findstr :端口` |
| 机器人有时回有时不回 | 跑了多个 gateway | `stop-gateway.cmd` 后只启动一个 |
| 控制台 Router 显示「待机」黄条 | router 空闲 60s 自动退出属正常；黄条是上次启动失败的历史记录 | 新消息到达会自动重试；traceback 可展开查看 |
| 收到事件但不回复 | router 消费失败 / 模型报错 | 看 `DATA_DIR/logs/router-*.log` 与 `worker-*.log`；lag 排查见 `docs/OPERATIONS.md` §4 |
| 重启机器后机器人不工作 | 本方案不做开机自启 | 手动 `start-gateway.cmd`（Redis/PG 若是服务则自启） |

## 6. 安全与开发约束

- 控制台**只读、无鉴权**：保持 127.0.0.1 绑定；需要远程访问自行加反向代理与鉴权。
- 密钥只进 `.env`（或 `ASSISTANT_ENV_FILE` 指向的仓库外文件）；日志对 key 脱敏（只显示 `sk-xxxx…末4位`）。不要提交 `.env`、`*.pem`、`*.key` 及任何密钥备份文件。
- 标 ★ 的文件为**冻结契约文件**（`contracts.py`、`config.py`、`bus/envelope.py`、`channel/base.py`、`agents/context.py`、`agents/vision.py`、`requirements.txt`、`.env.example`、`.gitignore`、`docs/ENGINEERING_CONTRACT.md`）：修改须走 `docs/ENGINEERING_CONTRACT.md` §11 变更流程，部署时**不要改**。
- SQLite 业务表只由 router 写入；控制台以只读模式打开。不要给 Redis 增加新的状态键。
- 二次开发验收：`scripts/check_contracts.py`、`check_store.py`、`check_adapter.py` 等（需要真实 Redis/Postgres 与运行数据）。

## 7. 文档索引

| 文档 | 内容 |
|---|---|
| `docs/OPERATIONS.md` | Windows gateway 运维手册（启动/停止/日志/三步排查）。注意：其中路径为原作者机器绝对路径，移植时以本文 2.7 为准 |
| `docs/COMMANDS.md` | Redis/进程排查命令速查（示例基于 WSL redis-cli，原生部署直接用 redis-cli 即可） |
| `docs/ENGINEERING_CONTRACT.md` | 工程契约：架构、冻结文件、变更流程 |
| `ui/sdk/`、`ui/web/` | 控制台协议 SDK（含 `asst-cli` 命令行）与前端源码 |