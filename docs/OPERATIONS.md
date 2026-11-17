# 运维手册（Gateway）

适用目录：`C:\Users\34897\Documents\Trae-CN\temp_20261108_06\personal-assistant`

本手册只覆盖**手动启动**的常驻 gateway（飞书长连接 + outbox 发送）。不包含计划任务、开机自启、Windows 服务、看门狗或自动重启——gateway 只在你手动启动后运行。

---

## 1. 首次配置

1. Python 环境（已建好可跳过）：

   ```
   C:\Users\34897\miniconda3\envs\feishu\python.exe
   ```

2. 项目根目录下的 `.env`（不要提交、不要改字段名）：

   | 变量 | 作用 |
   |---|---|
   | `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 飞书应用凭证，长连接必需 |
   | `DATA_DIR` | 运行数据根目录，当前为 `C:\Users\34897\Documents\Trae-CN\temp_20261108_06\assistant-data` |
   | `REDIS_URL` | Stream 总线（事件队列 / outbox / 去重 / 心跳） |
   | `DATABASE_URL` | Postgres（thread 状态、checkpoint） |

   验证配置能被读到：

   ```
   C:\Users\34897\miniconda3\envs\feishu\python.exe -c "import config; s=config.settings; print(s.data_dir); print(s.channel_kind)"
   ```

3. 依赖 Redis / Postgres 已启动；飞书后台已开启「长连接」事件订阅（不需要公网 IP）。

---

## 2. 日常启动 / 停止 / 查看

脚本目录：`C:\Users\34897\Documents\Trae-CN\temp_20261108_06\personal-assistant\scripts`

| 操作 | 双击文件 | 等价命令 |
|---|---|---|
| 启动 | `start-gateway.cmd` | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\gateway.ps1 -Action start` |
| 停止 | `stop-gateway.cmd` | `... gateway.ps1 -Action stop` |
| 状态 | `status-gateway.cmd` | `... gateway.ps1 -Action status` |
| 重启 | — | `... gateway.ps1 -Action restart` |
| 跟日志 | — | `... gateway.ps1 -Action logs -Follow` |

**窗口可以关闭**：`start` 通过 WMI（`Win32_Process.Create`）让 Windows 服务 `WmiPrvSE.exe` 创建 python 进程，它不是当前终端的子进程，所以关掉启动窗口（或关掉 Trae）不会杀掉 gateway。启动后进程链形如：

```
python.exe <- cmd.exe <- WmiPrvSE.exe <- svchost.exe <- services.exe
```

启动脚本会等到日志里出现 `connected to wss` 才打印 `READY`（首次启动要 import `lark_oapi`，约 50–60 秒）。看到 `READY` 就可以关窗口。

单实例：`start` 会先扫描 `python.exe` 且命令行含 `main_gateway.py` 的进程；已有实例时**拒绝启动**并提示。原因：飞书把同一条事件只投递给同一应用的**一条**长连接，多实例会让消息被随机分流，表现为「有时回有时不回」。

停止只杀「python 可执行文件 == `C:\Users\34897\miniconda3\envs\feishu\python.exe` 且命令行含 `main_gateway.py`」的进程，不会误杀其它 python。若任务管理器里手动结束，之后用 `status` 确认即可。

---

## 3. 日志

| 项 | 位置 |
|---|---|
| 当前日志 | `C:\Users\34897\Documents\Trae-CN\temp_20261108_06\personal-assistant\logs\gateway-live.log` |
| 历史（2026-11-09 21:23 前的旧编码） | `logs\gateway-live.20261109-pre-utf8.log` |
| 路由器日志 | `C:\Users\34897\Documents\Trae-CN\temp_20261108_06\assistant-data\logs\router-YYYY-MM-DD.log` |
| worker 日志 | `...\assistant-data\logs\worker-<task_id>.log` |
| 进程信息 | `logs\gateway.pid`（start 写入，stop 删除） |

日志统一 UTF-8（启动包装脚本里设置了 `PYTHONUTF8=1` / `PYTHONIOENCODING=utf-8`）。

常用关键字：

- `收到原始事件`：飞书事件到达
- `入队`：写入 router stream
- `已拉起 router` / `router 正常退出（空闲）`
- `connected to wss`：长连接建立
- `发送成功 ... duration_ms=`：outbox 发送完成

---

## 4. 「机器人不回消息」三步排查

按顺序做，不要跳过：

### 第一步：进程是否活着

```
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\gateway.ps1 -Action status
```

或直接看：

```
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*main_gateway.py*' } |
  Select-Object ProcessId, CreationDate, CommandLine
```

- 没有输出 → 进程已死，重新双击 `start-gateway.cmd`，再看日志末尾 30 行找异常。
- 有多个输出 → 多实例，先 `stop-gateway.cmd` 再启动一次。

### 第二步：lag 是否有积压

`status` 会打印 outbox / router 两个 stream 的 `xlen / lag / pending`。也可手动：

```
C:\Users\34897\miniconda3\envs\feishu\python.exe -c "import redis,config,json;r=redis.from_url(config.settings.redis_url,decode_responses=True);from bus.envelope import OUTBOX_STREAM,ROUTER_STREAM;print(json.dumps({'outbox':r.xinfo_groups(OUTBOX_STREAM),'router':r.xinfo_groups(ROUTER_STREAM)},ensure_ascii=False,default=str))"
```

判断：

- `router.lag > 0` 持续不降 → 事件已入队但没被消费：看 router 日志，确认是否被异常打断；gateway 会在下次检测时再拉起。
- `outbox.lag > 0` 且 `pending = 0` → 回复已生成但 gateway 没发出去：确认进程活着、飞书长连接正常；重发靠重新启动 gateway 消费积压。
- `pending > 0` → 有事件被投递但未 ACK，通常是 router 崩溃，下次拉起会 `XAUTOCLAIM` 认领。

### 第三步：长连接是否建立

```
Get-Content logs\gateway-live.log -Tail 30 -Encoding UTF8
```

- 最后一条 `connected to wss ... [conn_id=...]` 的时间应晚于最后一次 `启动 pid=`；否则说明还在建连（启动后 50–60s 内正常）或已掉线。
- 日志里出现 `启动 pid=` 但一直没有 `connected to wss` → 网络/凭证问题，检查 `.env` 的 `FEISHU_APP_ID/SECRET`、代理与防火墙。
- 有 `connected to wss` 但仍不回 → 回到第二步看 lag；三者都正常则检查飞书后台事件订阅是否指向同一个应用。

---

## 5. 常见故障速查

| 现象 | 可能原因 | 处理 |
|---|---|---|
| 双击 start 时提示 `Refusing to start: N gateway instance(s)` | 已有实例在跑 | 先 `stop-gateway.cmd`；确实要换新实例才停旧的 |
| `start` 打印 `WARNING: no websocket line within 150s` | 网络不可达 / 凭证错误 / 依赖导入失败 | 看日志末尾报错；确认飞书长连接可用、`.env` 正确 |
| 日志无「收到原始事件」 | 长连接未建立，或飞书事件订阅未开启 | 先看 `connected to wss`；再看飞书后台事件订阅 |
| 收到事件但 `outbox` 不增长 | router 消费失败或模型/工具报错 | 看 `router-YYYY-MM-DD.log` 与 `worker-*.log` |
| 进程莫名消失 | 曾被 `Start-Process`/终端启动（job object 回收），或机器重启 | 用 `start-gateway.cmd`（WMI 方式）；本方案不做开机自启，重启后需手动启动 |
| Redis 报错 / lag 查询失败 | Redis 未启动或 `REDIS_URL` 错 | 先启动 Redis，再 `status` 复检 |

---

## 6. 不要做的事

- 不要用 `Start-Process`、`& python main_gateway.py`、终端里直接前台运行来长期跑 gateway：这些进程属于当前终端的 job object，关窗口/会话就会一起被回收。
- 不要同时开两个 gateway：飞书长连接是单播分流，会随机丢消息。
- 不要修改 `.env` 里的 `DATA_DIR` 指向不同目录后再启动，否则会看到「没有历史会话」。
