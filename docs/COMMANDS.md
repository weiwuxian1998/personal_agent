# 命令速查（实测版）

> 本文所有命令都在本机实跑过，路径、参数、工作目录与仓库当前状态一致。
> 标注 **`[Win]`** = Windows PowerShell；**`[WSL]`** = 在 WSL 里执行（用 `wsl.exe` 外壳从 PowerShell 调用）。

## 0. 先设两个变量（每个新窗口执行一次）

```powershell
$P  = 'D:\personal_agent'
$PY = 'C:\Users\34897\miniconda3\envs\feishu\python.exe'
```

下面的命令都基于这两个变量；`$PY` 是唯一受支持的 Python 解释器。

---

## 1. 启停 gateway `[Win]`

`scripts\gateway.ps1` 的 `-Action` 合法值（`ValidateSet` 写死，其它值直接报错）：

| 值 | 作用 |
|---|---|
| `start` | 启动（通过 WMI 创建，脱离终端，关窗口不会死） |
| `stop` | 停止（只杀 `python 路径 == $PY` 且命令行含 `main_gateway.py` 的进程） |
| `restart` | 先 stop 再 start |
| `status` | 打印进程 / 长连接 / Redis stream / 末尾日志（**默认动作**，不带参数就等于 status） |
| `logs` | 打印日志尾部；加 `-Follow` 实时跟踪 |

参数：`-Tail <行数>`（默认 40）、`-Follow`（仅对 `logs` 生效）。

```powershell
# 启动（首次启动要等 READY，最长 150 秒：import lark_oapi 约 50–60 秒）
powershell -NoProfile -ExecutionPolicy Bypass -File "$P\scripts\gateway.ps1" -Action start

# 停止
powershell -NoProfile -ExecutionPolicy Bypass -File "$P\scripts\gateway.ps1" -Action stop

# 重启
powershell -NoProfile -ExecutionPolicy Bypass -File "$P\scripts\gateway.ps1" -Action restart

# 查状态
powershell -NoProfile -ExecutionPolicy Bypass -File "$P\scripts\gateway.ps1" -Action status

# 看日志 / 实时跟踪
powershell -NoProfile -ExecutionPolicy Bypass -File "$P\scripts\gateway.ps1" -Action logs -Tail 80
powershell -NoProfile -ExecutionPolicy Bypass -File "$P\scripts\gateway.ps1" -Action logs -Tail 20 -Follow
```

- **不需要管理员权限**。`Win32_Process.Create` 与 `Stop-Process`（自身会话内的进程）都不需要提权。
- 工作目录无所谓：脚本里把 `$ProjectDir` 写死了。
- 已有实例时 `start` 会**拒绝启动**（退出码 2），必须先 `stop`。多实例会导致飞书事件随机分流。

---

## 2. 手动拉起 / 停止 router `[Win]`

不走 gateway，手工起一个 router（排查用）。**工作目录必须是项目根**，否则 `import config` 失败。

```powershell
Set-Location $P
& $PY -u main_router.py
```

参数：

| 参数 | 作用 |
|---|---|
| `--once` | 处理一批事件后退出（联调用） |
| `--console` | 用控制台渠道，不连飞书 |

怎么让它退出：

- `Ctrl+C`（正常退出）；
- 没有新事件且 PEL 为空、持续 `router_idle_exit_s`（默认 60 秒）→ **自动退出，退出码 0**；
- 或 `Get-CimInstance Win32_Process ... | Stop-Process -Force`（见第 4 节）。

启动成功的确认：router 日志第一行会打印

```
启动 pid=... channel=feishu idle_exit=60s stream=asst:stream:router
```

日志路径见第 3 节。

> 起不来时先跑第 11 节的两条 import 自检（`import main_router`），那是最常见的一类原因。

> 注意：手动起的 router 不受 gateway 的 `_failures` 熔断约束，但**不要和 gateway 同时起第二个**，否则同一批事件会被两个消费者分抢。

---

## 3. 日志在哪 `[Win]`

| 日志 | 路径 | 说明 |
|---|---|---|
| gateway 主日志 | `$P\logs\gateway-live.log` | stdout+stderr 合并写入，UTF-8 |
| gateway 错误日志 | `$P\logs\gateway-live.err.log` | 实际为 0 字节（`.cmd` 里已 `2>&1` 合并） |
| 旧编码历史 | `$P\logs\gateway-live.20261109-pre-utf8.log` | 2026-11-09 21:23 之前 |
| router | `$env:USERPROFILE\.assistant\logs\router-YYYY-MM-DD.log` | **按上海时间每天一个**，追加写 |
| worker | `$env:USERPROFILE\.assistant\logs\worker-<task_id>.log` | 每任务一个 |
| 进程信息 | `$P\logs\gateway.pid` | `start` 写入，`stop` 删除 |

```powershell
# 看尾部
Get-Content "$P\logs\gateway-live.log" -Tail 50 -Encoding UTF8

# 实时跟踪（Ctrl+C 退出）
Get-Content "$P\logs\gateway-live.log" -Tail 20 -Wait -Encoding UTF8

# router 今天的日志
Get-Content "$env:USERPROFILE\.assistant\logs\router-$(Get-Date -Format yyyy-MM-dd).log" -Tail 50

# 按关键字抓（日志已有 170MB+，不要全量 Get-Content）
Select-String -Path "$P\logs\gateway-live.log" -Pattern "已拉起 router|router 异常退出|router 正常退出|发送成功" |
  Select-Object -Last 10 | ForEach-Object { "$($_.LineNumber): $($_.Line.Trim())" }
```

---

## 4. 查 router 是否在跑 / 是否熔断

### 4.1 进程 `[Win]`

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*main_router.py*' } |
  Select-Object ProcessId, ParentProcessId, CreationDate, CommandLine | Format-List
```

无输出 = 当前没在跑。

### 4.2 心跳键 `[WSL]`

```powershell
wsl.exe -d Ubuntu-24.04 -- bash -lc "redis-cli get asst:router:heartbeat; redis-cli ttl asst:router:heartbeat"
```

有值且 TTL > 0 = router 活着；`(nil)` / `-2` = 没在跑（空闲退出后心跳会随 TTL 过期消失，属正常）。

### 4.3 熔断（关键，容易看错）

日志里只有这三类关键字：

- `已拉起 router pid=<pid>（第 N 次），日志 <path>`
- `router 异常退出 code=<X>（连续 N 次），<S>s 后重试；详情见 <path>`
- `router 正常退出（空闲）`

失败重试退避是 `2 / 5 / 10 / 20 / 30` 秒；连续失败达到 **5 次后代码直接 `return`，不打印任何"放弃/熔断"日志**（日志里已实锤过一次：`2026-11-10 20:29:50 router 异常退出 code=2（连续 5 次）` 之后再无任何拉起，直到 22:13 重启 gateway）。

另外两个关键字也说明"它想拉起但拉不起来"：

- `supervisor tick 失败` —— Redis 不可达（WSL 停了），**这种情况下根本计数不到 5 次**，日志会每秒刷这一条，别误判为"没有熔断"；
- `拉起 router 失败` —— `subprocess.Popen` 本身抛异常（解释器路径/权限问题）。

所以判断熔断的唯一方法是：

```powershell
Select-String -Path "$P\logs\gateway-live.log" -Pattern "已拉起 router|router 异常退出|router 正常退出" |
  Select-Object -Last 8 | ForEach-Object { $_.Line.Trim() }
```

**如果最近一条是「第 5 次」，并且之后既没有新的「已拉起 router」、也没有 router 进程 → 已熔断。**

复位方式（`_failures` 只存在 gateway 内存里）：

- router 正常退出（code 0）会清零；
- 否则只能**重启 gateway**（`-Action restart`）；
- 熔断状态下即便把根因修好，也要重启 gateway 才会再尝试拉起。

---

## 5. 查 worker `[Win]`

### 5.1 在跑的 worker

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*worker_runner.py*' } |
  Select-Object ProcessId, CreationDate, CommandLine | Format-List
```

### 5.2 某个任务的结果

```powershell
$task = '<task_id>'
Get-Content "$env:USERPROFILE\.assistant\workers\$task\result.json" -Raw -Encoding UTF8
Get-Content "$env:USERPROFILE\.assistant\workers\$task\task.json"   -Raw -Encoding UTF8
```

### 5.3 DB 里 `running` 但进程已消失的僵尸行

走 Store 接口（推荐）。**必须 `Set-Location $P`**，否则 `import config` 找不到：

```powershell
Set-Location $P
& $PY -c 'import config; from store.sqlite import Store; s=Store(config.settings.db_path); [print(r.id, r.status, r.pid, r.updated_at) for r in s.list_sessions(status="running")]'
```

实测输出示例（当前就有一条僵尸）：

```
worker:01M70813KTKV83AQGFKW07EQ1W running 28656 2026-11-09T21:30:13.008+08:00
```

再用 `Get-CimInstance Win32_Process -Filter "ProcessId=28656"` 确认进程不存在 → 即僵尸行。

> 不要用 DB Browser / sqlite 客户端以可写方式长时间占用 `assistant.db`。库是 WAL 模式，`assistant.db-wal` / `-shm` 在 checkpoint 后自动合并消失属正常现象，不是数据丢失。

---

## 6. Redis 与两条 stream `[WSL]`

Redis **只跑在 WSL 里**（`127.0.0.1:6379`，Windows 侧是 WSL 的端口转发）。统一外壳：

```powershell
wsl.exe -d Ubuntu-24.04 -- bash -lc "<命令>"
```

（首次调用会打印一行 `wsl: 检测到 localhost 代理配置…` 的告警，无害，忽略。）

```powershell
# 连通性
wsl.exe -d Ubuntu-24.04 -- bash -lc "redis-cli ping"

# 心跳 / 拉起锁
wsl.exe -d Ubuntu-24.04 -- bash -lc "redis-cli get asst:router:heartbeat; redis-cli ttl asst:router:heartbeat; redis-cli exists asst:router:spawning"

# 长度
wsl.exe -d Ubuntu-24.04 -- bash -lc "redis-cli xlen asst:stream:router; redis-cli xlen asst:stream:outbox"

# 消费组：lag / pending / consumers / last-delivered-id
wsl.exe -d Ubuntu-24.04 -- bash -lc "redis-cli xinfo groups asst:stream:router"
wsl.exe -d Ubuntu-24.04 -- bash -lc "redis-cli xinfo groups asst:stream:outbox"

# 消费者明细（第二个参数是**组名**，不是消费者名）
wsl.exe -d Ubuntu-24.04 -- bash -lc "redis-cli xinfo consumers asst:stream:router router"
wsl.exe -d Ubuntu-24.04 -- bash -lc "redis-cli xinfo consumers asst:stream:outbox gateway-sender"

# 未 ACK 明细（没有则显示 0）
wsl.exe -d Ubuntu-24.04 -- bash -lc "redis-cli xpending asst:stream:router router"
wsl.exe -d Ubuntu-24.04 -- bash -lc "redis-cli xpending asst:stream:outbox gateway-sender"
```

组名 / 消费者名固定值（来自 `bus/envelope.py`，别猜）：

| stream | 组名 | 消费者名 |
|---|---|---|
| `asst:stream:router` | `router` | `router-1` |
| `asst:stream:outbox` | `gateway-sender` | `gateway-1` |

一条命令看完两条 stream（**刻意写成不含 shell 变量的形式**，原因见下方注意）：

```powershell
wsl.exe -d Ubuntu-24.04 -- bash -lc 'echo "--router--"; redis-cli xlen asst:stream:router; redis-cli xinfo groups asst:stream:router | tr "\n" " "; echo; echo "--outbox--"; redis-cli xlen asst:stream:outbox; redis-cli xinfo groups asst:stream:outbox | tr "\n" " "; echo'
```

实测输出：

```
--router--
148
name router consumers 1 pending 0 last-delivered-id ... entries-read 263 lag 1
--outbox--
129
name gateway-sender consumers 1 pending 0 last-delivered-id ... entries-read 129 lag 0
```

> **注意（实测坑）**：`wsl.exe ... bash -lc '...'` 里写 shell 变量（`$s`、`$x`）会被外层吞掉，`for ... do ... done` 尤其容易踩；
> 双引号外壳还会让内层引号错乱（报 `sed: unknown command: '"'`）。本节命令因此全部不含变量，
> 需要变量时就把脚本落到 WSL 里的 `.sh` 文件再执行（记得先 `sed -i 's/\r$//'`）。

判读：

| 现象 | 含义 |
|---|---|
| `router` 的 `lag > 0` 持续不降 | 事件已入队但没人消费 → router 没起来 / 一启动就崩 |
| `router` 的 `pending > 0` | 有事件被投递但未 ACK → 上一个 router 崩了，下次拉起会 `XAUTOCLAIM` |
| `outbox` 的 `lag > 0` 且 `pending = 0` | 回复已生成但 gateway 没发出去 → 查 gateway 进程与长连接 |

---

## 7. 模型直连 `[Win]`（本地网关已于 2026-11-17 移除）

助理直连 DeepSeek 官网，`.env` 关键三行：

```ini
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-flash
LLM_API_KEY=***          # DeepSeek 官方 key
```

验证一次真实调用（用项目自己的装配函数，确认地址与模型生效）：

```powershell
Set-Location $P
& $PY -c "from config import settings; from agents.base import build_chat_model; m=build_chat_model(settings); print(settings.llm_base_url, settings.llm_model); print(m.invoke('只回复两个字：正常').content)"
```

---

## 8. WSL 是否活着 `[Win] / [WSL]`

```powershell
# 1) 虚拟机状态
wsl.exe -l -v          # STATE 应为 Running；Stopped 表示已关

# 2) 两个依赖（两步都过才算完整）
wsl.exe -d Ubuntu-24.04 -- bash -lc "redis-cli ping"                                   # PONG
wsl.exe -d Ubuntu-24.04 -- bash -lc "systemctl is-active postgresql"                   # active
```

**重要：`wsl.exe -l -v` 只是列状态、不会唤醒；但任何 `wsl.exe -d <发行版> -- <命令>` 都会把已经 Stopped 的 VM 拉起来。** 所以"先看 Stopped、下一条命令又能跑"是正常的：第二条命令本身就是唤醒动作。

被唤醒后要按这个顺序等（本次实测）：

1. `redis-cli ping` 基本立刻 `PONG`；
2. `systemctl is-active postgresql` 立刻 `active`（没有自动启动的机器上要手动 `systemctl start postgresql`）。

依赖关系（WSL 一停，整条链全断）：

```
WSL(Ubuntu-24.04)
 ├── redis-server     127.0.0.1:6379   ← 事件队列/outbox/去重/心跳
 └── postgresql       127.0.0.1:5432   ← LangGraph checkpoint（库 agent_skel）
```

WSL 停着的时候，网关日志会以约 3 秒一次刷
`redis.exceptions.ConnectionError: Error 10061 connecting to 127.0.0.1:6379`，
**日志体积会暴涨（本次已从 29.5MB 涨到 173MB）**，这是症状不是病根。

---

## 9. 只读体检组（随时可跑，不改状态）

| 命令 | 用途 |
|---|---|
| `powershell -File scripts\gateway.ps1 -Action status` | 进程 + 长连接 + stream + 日志 |
| `... -Action logs -Tail N` | 看日志（不加 `-Follow`） |
| `Get-CimInstance Win32_Process ...` 各段 | 查 gateway / router / worker 进程 |
| `Select-String -Path ... -Pattern ...` | 关键字检索日志 |
| `wsl.exe -l -v` | WSL 是否 Running（**纯读取，不唤醒**） |
| `redis-cli ping / xlen / xinfo groups / xpending / get / ttl` | 队列与心跳 |
| `systemctl is-active / show` | 服务状态 |
| `Store.list_sessions(...)` / 只读 SQL | 会话与僵尸行 |

## 10. 会改变状态的命令（谨慎）

| 命令 | 影响 |
|---|---|
| `gateway.ps1 -Action start / stop / restart` | 启停 gateway，长连接断开重连 |
| `& $PY -u main_router.py`（含 `--once`） | 起一个新 router 消费事件 |
| `Stop-Process`（router/worker） | 杀进程，未 ACK 事件留在 PEL |
| `wsl.exe -d Ubuntu-24.04 -- <命令>`（**任意命令**） | 会唤醒 Stopped 的 WSL，Redis/Postgres 随之启动（`wsl.exe -l -v` 例外，它不唤醒） |
| 手动 `XAUTOCLAIM` / `XACK` | 会改动消费组状态，排查时不要随手敲 |

---

## 11. 已知坑：router 曾因 psycopg DLL 被拦截而起不来（附当前复测结论）

**历史现象（11-11 22:53 实测）**：手动跑第 2 节、或等 gateway 自动拉起，router 都在几秒内退出，`router-2026-11-11.log` 末尾就是这段栈：

```
Traceback (most recent call last):
  File "...\main_router.py", line 49, in <module>
    from store.checkpointer import open_checkpointer
  File "...\store\checkpointer.py", line 19, in <module>
    from langgraph.checkpoint.postgres import PostgresSaver
  ...
ImportError: no pq wrapper available.
Attempts made:
- couldn't import psycopg 'c' implementation: No module named 'psycopg_c'
- couldn't import psycopg 'binary' implementation: DLL load failed while importing pq: 应用程序控制策略已阻止此文件。
- couldn't import psycopg 'python' implementation: libpq library not found
```

**根因**：`psycopg` 的 `pq` DLL 被 Windows 应用程序控制策略拦截（环境问题，不是参数/路径问题）。

**但 11-12 22:00 复测已经恢复正常**，两条都过：

```powershell
Set-Location $P
& $PY -c "import store.checkpointer; print('checkpointer ok')"
& $PY -c "import main_router; print('main_router ok')"
```

现在第二条会打印 `main_router ok`（历史日志里的那段栈仍在 `router-2026-11-11.log` 里，别拿旧日志当现状）。
所以起 router 前先跑这两条自检：**报 `no pq wrapper` = 拦截还在（先修环境）；打印 ok = 可以按第 2 节正常起**。

**拦截还在时，gateway 日志里的样子**：

```
INFO  已拉起 router pid=xxx（第 3 次），日志 ...\router-2026-11-11.log
ERROR router 异常退出 code=1（连续 3 次），10s 后重试；详情见 ...
... 一直数到「连续 5 次」之后彻底安静（熔断，且没有"放弃"日志）
```

**额外提醒（实测）**：如果 Redis 也不可达（WSL 停着），`supervisor tick 失败` 会每秒刷屏，计数停在中途到不了 5 次，
这时的安静**不是**熔断，而是它连"有没有活干"都读不出来。先按第 8 节把 WSL 弄活，再判断熔断。

**当前状态（2026-11-12 22:00 核对）**：gateway 没在跑（11-12 01:43 机器重启后未启动，
`-Action status` 输出 `Process : NOT RUNNING`，`gateway.pid` 里还是旧的 `pid=36176`）。
所以完整顺序是：`-Action start` → 观察日志里是否出现 `已拉起 router` → 再看 router 日志首行有没有 `启动 pid=`。

**不要做的事**：不要以为是参数/路径写错而反复 `start`；若确实撞上 5 次熔断，即使把根因修好也必须**重启 gateway** 复位 `_failures`（否则它再也不会尝试拉起 router）。

---

## 12. 与 `docs/OPERATIONS.md` 的差异（已核对）

| # | OPERATIONS.md 的说法 | 实际情况 |
|---|---|---|
| 1 | 依赖项写作 `DATABASE_URL`（Postgres） | `.env` 里是 **`POSTGRES_DSN`**，没有 `DATABASE_URL`；另外业务表在 **SQLite**（`assistant-data\db\assistant.db`），只有 checkpoint 在 Postgres |
| 2 | "依赖 Redis / Postgres 已启动" | 没写它们**都在 WSL 里**；WSL 一停全部失联。文档全文没有任何 `wsl.exe` 命令 |
| 3 | 未提及模型直连 | `.env` 现为 `LLM_BASE_URL=https://api.deepseek.com/v1`、`LLM_MODEL=deepseek-flash`（2026-11-17 起直连；此前经本地网关） |
| 4 | 故障表写 "gateway 会在下次检测时再拉起" | 连续失败 **5 次后静默停止**（代码只 `return`，无日志），文档没写熔断与复位方式 |
| 5 | `logs\gateway-live.20261109-pre-utf8.log` | ✅ 存在（9133 字节），一致 |
| 6 | `router-YYYY-MM-DD.log`、`worker-<task_id>.log` | ✅ 与代码一致（router 按上海时间分日） |
| 7 | 只写了 `-Action logs -Follow` | 还有 `-Tail <N>`；`-Action` 合法值为 `start/stop/restart/status/logs`，默认 `status` |
| 8 | 未提手动起 router / `--once` / `--console` | 均可用，见本文第 2 节 |
