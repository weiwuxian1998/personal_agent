# 个人助理两层架构 · 工程契约 v1

> 状态：**已冻结，可并行开发**  
> 依据：`personal-assistant-two-layer-architecture.md`（v5，0 条待决策）  
> 仓库：`personal-assistant`（main 为基线）  
> 本文档是并行开发的**唯一接口依据**。实现与本文档冲突时，以本文档为准；本文档与设计文档冲突时，按 §14 的取舍执行，并把新发现补进 §14。

---

## 0. 契约效力

1. 本文档定义**模块边界**：谁能改哪些文件、模块之间只允许通过哪些符号交互。
2. **冻结文件**（§11）只能由主线修改；其他模块需要新字段/新参数时，提交"契约变更请求"（说明字段名、类型、默认值、影响面），由主线统一改并通知所有分支 rebase。
3. 每个模块必须自带一个可执行验收脚本 `scripts/check_<module>.py`，**退出码 0 才算交付**。
4. 不允许为了让验收通过而写空实现/假数据；跑不通就如实报告卡点。

---

## 1. 运行环境（已实测，不要改）

| 项 | 值 |
|---|---|
| 解释器 | `C:\Users\34897\miniconda3\envs\feishu\python.exe`（Python 3.11.18） |
| LangChain | `langchain 1.4.9` + `langchain-core 1.7.6`（`create_agent` 已验证可导入） |
| LangGraph | `langgraph 1.2.20`，`langgraph-checkpoint-postgres 3.2.0` |
| Redis | 本机 7.0.15，Windows 经 `127.0.0.1:6379` 访问 |
| Postgres | 本机 16.15，DSN 在 `.env` 的 `POSTGRES_DSN` |
| Shell | 默认 `pwsh` 7.6.9（per-user MSIX，路径见 §6.2 探测） |
| 工作区 | 默认 `%USERPROFILE%\assistant-workspace`，运行数据默认 `%USERPROFILE%\.assistant` |

**禁止**：新增第三方依赖（先提需求）；把真实密钥写进代码/日志/JSONL；修改 `temp_20261108_02\feishu_langgraph_skeleton`（有监听进程在跑）。

---

## 2. 进程与消息边界

```
飞书 → main_gateway.py（唯一常驻）
        ├─ FeishuChannel.run_forever  → 去重(SETNX) → XADD asst:stream:router
        └─ supervisor 线程（不 import LLM）：每 1s 判心跳/待处理；每 5s 扫 schedules；
                                              每 5min 扫 sessions（pid + checkpoint 停滞）
                     │ spawn
                     ▼
main_router.py（按需拉起，空闲 60s 退出，thread_id=router:main）
        ├─ XREADGROUP batch → render_digest → create_agent 一轮
        ├─ 工具：ReplyUser（唯一用户出口）/ create_session / send_message_to_session /
        │        list_sessions / read_session / TaskOutput / StopCommand / Schedule /
        │        Read / Write / Edit / DeleteFile / Shell(≤30s) / Skill / assistant_profile /
        │        set_session_title / fork_session / ToolSearch / DeferExecuteTool
        └─ 一轮结束 → JSONL fsync → XACK → 空闲退出
                     │ create_session: subprocess Popen(worker_runner.py --task <id>)
                     ▼
worker_runner.py（每任务一进程，跑完即退，thread_id=worker:<task_id>）
        ├─ create_agent + middleware + PostgresSaver
        ├─ 工具：Read/Write/Edit/DeleteFile/Shell(≤600s)/Glob/Grep/SendMessage/
        │        WebSearch/WebFetch/TodoWrite/new_context/Skill/StopCommand(本进程后台命令)
        └─ 结束 → XADD worker_result{task_id,text} → 退出
```

**铁律**
1. `ReplyUser` 是 router 唯一用户出口；worker 没有该工具。
2. worker 结束只回传 `{task_id, text}`，不回传结构化状态。
3. 派发回执只能是 `status="accepted"`，禁止出现 `done/success/completed/result/output`。
4. router 不等待 worker：派发后本轮结束；worker 结果作为新事件唤醒下一轮。
5. worker→router 的消息复用 `asst:stream:router`，唤醒机制只有一条。

---

## 3. 共享数据结构 `contracts.py`（冻结）

现文件已提交，字段如下（**这是唯一来源，模块不得自建同义结构**）：

```python
TZ_SHANGHAI: timezone                        # +08:00
EVENT_TYPES = {user_message, worker_result, worker_message, worker_notice,
               router_message, timer, system}
EVENT_SOURCES = {feishu, worker, scheduler, router, system}
ToolProfile = Literal["router", "worker"]
SessionStatus = Literal["running","waiting_router","idle","stopped","error","done","unreachable"]

def ulid(ts_ms=None) -> str                  # 26 字符，时间有序
def now_iso() -> str                         # +08:00 ISO8601 毫秒
def now_utc_iso() -> str                     # UTC，Z 结尾
def parse_iso(s) -> datetime

@dataclass Event:
    type: str; source: str; payload: dict
    v: int = 1; id: str = ""; ts: str = ""; trace_id: str = ""; dedupe_key: str|None = None
    to_dict() / to_json() / from_dict() / from_json()

@dataclass IncomingMessage:
    channel, chat_id, chat_type, message_id, event_id, sender_id, text,
    create_time: int, thread_id: str|None, raw: dict

@dataclass SendResult:
    ok: bool; message_id: str = ""; error: str = ""; code: int|None = None

@dataclass SessionRecord:
    id, title, status, cwd, pid, pid_started_at, parent_id,
    created_at, updated_at, first_message_at     # from_row(row)

@dataclass ScheduleRecord: ...                   # from_row(row)

@dataclass TaskSpec:                             # workers/<task>/task.json
    task_id, prompt, title, trace_id, cwd, workspace_root,
    tools_profile="worker", task_kind="general", resume=False, created_at
    to_dict() / to_json() / from_dict() / from_json()

def tool_error(code, message, *, hint="", **extra) -> dict   # {"ok":False,...}
```

**命名说明**：不建 `types.py`（会遮蔽标准库 `types`）。

---

## 4. Redis 协议（`bus/envelope.py` 冻结 + `bus/stream.py` 由 M-STORE 实现、主线维护）

### 4.1 Key 常量（已提交）

```python
ROUTER_STREAM = "asst:stream:router"; ROUTER_GROUP = "router"; ROUTER_CONSUMER = "router-1"
ROUTER_STREAM_MAXLEN = 10000; WORKER_STREAM_MAXLEN = 200
ROUTER_HEARTBEAT_KEY; ROUTER_SPAWNING_KEY; ROUTER_STATE_KEY
DEDUPE_FEISHU_PREFIX; REPLY_IDEM_PREFIX; SESSION_LOCK_PREFIX; QUESTION_ANSWERED_PREFIX
worker_stream(task_id) / worker_group(task_id) / worker_consumer(task_id)
stop_key(task_id) / inflight_key(task_id) / retry_key(task_id)
session_lock_key(session_id) / question_answered_key(question_id)
```

### 4.2 信封

Redis entry 只有一个字段 `data`，值为 `Event.to_json()`；entry id 由 Redis 生成。

```python
new_event(type, source, payload, *, trace_id=None, dedupe_key=None, event_id=None) -> Event
event_to_redis(e) -> {"data": json}
event_from_redis(fields) -> Event
render_digest(batch, *, now=None) -> str     # §4.5 的唤醒摘要
```

payload 字段（按 type）：

| type | payload |
|---|---|
| `user_message` | `message_id, text, chat_id, chat_type?` |
| `worker_result` | `task_id, text` |
| `worker_message` | `task_id, question_id, text, await_reply, deadline_s, blocked_on?, attempted?` |
| `worker_notice` | `task_id, kind ∈ {progress,blocked}, detail` |
| `router_message` | `task_id, reply_to, text` |
| `timer` | `schedule_id, scheduled_at, prompt` |
| `system` | `kind`（`task_watch` 时带 `task_id, reason`） |

### 4.3 `Bus` 类（`bus/stream.py`，M-STORE 实现，主线维护）

```python
class Bus:
    def __init__(self, client: redis.Redis): ...
    def ensure_router_group(self) -> None
    def ensure_worker_group(self, task_id: str) -> None
    def worker_consumer_name(self, task_id: str) -> str       # = "w-<task>-1"
    def xadd(self, event: Event, *, stream: str = ROUTER_STREAM, maxlen: int = ROUTER_STREAM_MAXLEN) -> str
    def read_batch(self, *, block_ms=5000, count=10) -> list[tuple[str, Event]]
    def drain_more(self, first, *, debounce_ms=200, max_batch=10) -> list[tuple[str, Event]]
    def ack(self, entries: list[tuple[str, Event]]) -> None
    def claim_pending(self, *, min_idle_ms=60000, count=10) -> list[tuple[str, Event]]
    def pel_nonempty(self) -> bool
    def lag(self) -> int                      # XINFO GROUPS 的 lag；组内尚未投递的事件数
    def xlen(self) -> int

# 模块级（不是 Bus 方法）：飞书 gateway 与 ReplyUser 共用的原子去重
def try_dedupe(client: redis.Redis, dedupe_key: str, *, ttl_s: int = 6*3600) -> bool
# dedupe_key 自动补 DEDUPE_FEISHU_PREFIX；返回 True = 第一次见到，可入队
```

- `try_dedupe` 用单条 `SET NX EX`，并发安全；入队失败必须删除该键，否则消息永久丢失；
- 阻塞读连接必须 `socket_timeout=None`（或 ≥ 2×block），与命令连接分开；
- `MAXLEN ~` 近似裁剪；
- 未 ACK 的条目留在 PEL，崩溃后 `XAUTOCLAIM`；
- **supervisor 判断「有没有活」必须用 `lag()` + `pel_nonempty()`，不能用 `XLEN`**：
  `XACK` 不删除条目，`XLEN` 永远大于 0，用它会反复拉起 router（已实测）。
  旧版 Redis 没有 `lag` 字段时，`lag()` 退化为 `pending` 数，只影响拉起时机。

---

## 5. 存储层

### 5.1 Postgres checkpoint（`store/checkpointer.py`，M-STORE）

```python
@contextmanager
def open_checkpointer(dsn: str, *, min_size=1, max_size=8) -> Iterator[tuple[ConnectionPool, PostgresSaver]]
```

要求：`autocommit=True`、`prepare_threshold=0`、`saver.setup()` 幂等；退出时关闭 pool。  
**只存 checkpoint，不建业务表**。

### 5.2 SQLite（`store/sqlite.py`，M-STORE）

配置：`PRAGMA journal_mode=WAL; synchronous=NORMAL; busy_timeout=5000`。  
**只允许 router 进程写**；worker 只允许调用 `record_tool_event` / `record_cost`（各自短连接）。

DDL 必须与下面完全一致（可加索引，不可改字段名/类型）：

```sql
CREATE TABLE sessions (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL,
  cwd TEXT, pid INTEGER, pid_started_at TEXT, parent_id TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  first_message_at TEXT
);
CREATE INDEX idx_sessions_status ON sessions(status, updated_at);

CREATE TABLE runs (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
  trigger TEXT NOT NULL, status TEXT NOT NULL,
  started_at TEXT NOT NULL, ended_at TEXT, exit_code INTEGER, turn_id TEXT
);

CREATE TABLE schedules (
  id TEXT PRIMARY KEY, cron TEXT, at TEXT,
  timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
  prompt TEXT NOT NULL, status TEXT NOT NULL,
  next_run_utc TEXT, last_run_utc TEXT,
  misfire_grace_s INTEGER NOT NULL DEFAULT 300,
  catch_up TEXT NOT NULL DEFAULT 'once',
  created_at TEXT NOT NULL
);
CREATE INDEX idx_sched_due ON schedules(status, next_run_utc);

CREATE TABLE task_watch (
  task_id TEXT NOT NULL REFERENCES sessions(id),
  reason TEXT NOT NULL, status TEXT NOT NULL,
  first_detected_at TEXT NOT NULL, last_detected_at TEXT NOT NULL,
  alerted_at TEXT, dedupe_key TEXT NOT NULL,
  PRIMARY KEY (task_id, reason)
);
CREATE UNIQUE INDEX idx_watch_dedup ON task_watch(dedupe_key);

CREATE TABLE cost_ledger (
  id TEXT PRIMARY KEY, ts_utc TEXT NOT NULL, trace_id TEXT,
  scope TEXT NOT NULL, model TEXT NOT NULL,
  input_tokens INTEGER NOT NULL, cached_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL, cost_usd REAL NOT NULL, price_version TEXT NOT NULL
);

CREATE TABLE pending_approvals (
  token TEXT PRIMARY KEY, trace_id TEXT, tool TEXT NOT NULL, args_json TEXT NOT NULL,
  created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
);

CREATE TABLE approval_log (
  id TEXT PRIMARY KEY, trace_id TEXT, tool TEXT NOT NULL, args_hash TEXT NOT NULL,
  args_json TEXT, decision TEXT NOT NULL, decided_at TEXT, note TEXT
);

CREATE TABLE tool_events (
  id TEXT PRIMARY KEY, trace_id TEXT, session_id TEXT, tool TEXT NOT NULL,
  shell TEXT, error_class TEXT, fingerprint TEXT,
  retry_count INTEGER NOT NULL DEFAULT 0, exit_code INTEGER,
  ok INTEGER NOT NULL, duration_ms INTEGER NOT NULL, args_hash TEXT, created_at TEXT NOT NULL
);
CREATE INDEX idx_tool_events_fp ON tool_events(fingerprint, created_at);
CREATE INDEX idx_tool_events_class ON tool_events(error_class, created_at);

CREATE TABLE allowed_roots (path TEXT PRIMARY KEY, added_at TEXT NOT NULL);
```

`Store` API（同步、可多线程调用；内部每次用短连接或线程本地连接）：

```python
class Store:
    def __init__(self, db_path: str | Path): ...
    def init_schema(self) -> None

    # sessions
    def upsert_session(self, session_id, *, title, status, cwd=None, parent_id=None,
                       pid=None, pid_started_at=None, first_message_at=None) -> None
    def set_pid(self, session_id, pid: int, pid_started_at: str) -> None
    def set_status(self, session_id, status: str) -> None
    def set_first_message_at(self, session_id, ts: str | None = None) -> None
    def get_session(self, session_id) -> SessionRecord | None
    def list_sessions(self, *, status=None, limit=20, cursor=None) -> list[SessionRecord]

    # runs
    def create_run(self, run_id, *, session_id, trigger, status, turn_id=None) -> None
    def finish_run(self, run_id, *, status, exit_code=None) -> None

    # schedules
    def create_schedule(self, *, cron=None, at=None, timezone="Asia/Shanghai", prompt,
                        misfire_grace_s=300, catch_up="once") -> ScheduleRecord
    def list_due_schedules(self, now_utc: str, limit=20) -> list[ScheduleRecord]
    def update_schedule_after_run(self, schedule_id, *, next_run_utc: str | None, ran_at_utc: str) -> None
    def set_schedule_status(self, schedule_id, status: str) -> None
    def list_schedules(self, *, status=None, limit=50) -> list[ScheduleRecord]

    # task_watch（返回 "first" 表示第一次命中，调用方据此 XADD）
    def mark_watch(self, task_id, reason, *, now: str | None = None) -> Literal["first","seen"]
    def resolve_watch(self, task_id, reason=None) -> None

    # 观测
    def record_tool_event(self, *, trace_id, session_id, tool, ok, duration_ms,
                          shell=None, error_class=None, fingerprint=None,
                          retry_count=0, exit_code=None, args_hash=None) -> None
    def record_cost(self, *, trace_id, scope, model, input_tokens, cached_tokens,
                    output_tokens, cost_usd, price_version="v1") -> None

    # 审批
    def create_pending_approval(self, *, token, trace_id, tool, args_json, ttl_s=600) -> None
    def decide_approval(self, token, decision: str, note: str = "") -> dict | None
```

时间字段统一 `ISO8601` 字符串；`next_run_utc` 用 `now_utc_iso()` 格式（UTC、Z 结尾），比较用 `parse_iso`。

```python
# 模块级（M-STORE 交付）：cron 计算，纯标准库 zoneinfo
def next_run_utc_after(cron: str, timezone_name: str, after: datetime | None = None) -> datetime
# 返回严格晚于 after 的下一次触发时刻（UTC datetime）；after 默认 now(UTC)
```

**cron 语法限制（v1）**：仅标准 5 段数字语法（`*`、`a`、`a-b`、`*/n`、`a-b/n`、逗号列表），不支持 `JAN`、`L`、`W`；非法表达式抛 `ValueError`。

### 5.3 JSONL（`memory/jsonl.py`，M-STORE）

```python
class JsonlWriter:
    def __init__(self, directory: Path, prefix: str)       # 文件按 Asia/Shanghai 日期旋转
    def next_seq(self) -> int                              # 扫描现有文件的最大 seq + 1
    def append(self, record: dict) -> int                  # 自动补 v/seq/ts；flush+fsync；返回 seq
def append_turn(turn: dict, *, directory: Path) -> int     # 便捷函数
def read_records(directory: Path, *, since_seq=0, limit=None) -> Iterator[dict]
```

写入必须 `open(..., "a", encoding="utf-8")` + 单行 JSON + `flush()` + `os.fsync()`；**永不回写旧记录**。

---

## 6. 渠道层

### 6.1 `channel/base.py`（冻结）

```python
MessageHandler = Callable[[IncomingMessage], None]
class Channel(ABC):
    name: str
    def send_text(*, chat_id, text, reply_to_message_id=None, thread_id=None) -> SendResult
    def run_forever(on_message: MessageHandler) -> None     # 阻塞
```

### 6.2 `channel/feishu.py`（M-CHANNEL）

- `parse_p2_message(data) -> IncomingMessage | None`：只收 `sender_type in (None, "user")`、`message_type=="text"`；`content` 是 JSON `{"text":...}`；按 `mentions[].key` 去掉 @ 占位符；空文本返回 None。
- `FeishuChannel(app_id, app_secret, domain)` 实现 `send_text`（有 `reply_to_message_id` 用 reply 接口，否则 create）与 `run_forever`（`lark.ws.Client` + `EventDispatcherHandler.builder("", "")`）。
- 回调中吞异常并记日志（避免飞书判 500 重推）；回调只做"去重 + XADD"，不得做 LLM/数据库长操作。
- 非文本消息（image/file）先记日志并跳过；图片解析留到 v1.1（契约不要求）。

### 6.3 `channel/console.py`（M-CHANNEL）

`ConsoleChannel.run_forever` 从 stdin 读行，构造 `IncomingMessage(channel="console", chat_id="console", message_id=ulid(), ...)`；`send_text` 打印到 stdout 并返回 `SendResult(ok=True, message_id=ulid())`。用于无飞书环境的闭环调试。

### 6.4 `channel/factory.py`（主线，新增）

```python
def build_channel(settings: Settings) -> Channel   # 读 settings.channel_kind：feishu | console
```

- gateway（`run_forever`）与 router（`send_text`）**必须**用同一个工厂选渠道，禁止在两个入口里各写一份。
- **必须惰性导入 `channel.feishu`**：`import lark_oapi` 实测 47.2 s（加载数千个 API 模型模块）。控制台模式与 worker 进程不应为飞书导入付启动代价。
- 未知 `channel_kind` 直接抛错，不静默降级。

### 6.5 channel → Redis 状态上报（已决定，v1 不做）

- **v1 不创建 `asst:channel:state` 之类的键。**
- 渠道健康只有两个出口：结构化日志（每条发送的 `ok/code/error/耗时`）与 `SendResult` 字段；渠道存活由 gateway 进程本身承担（进程在 = 渠道在）。
- 理由：单 gateway 单渠道下新增状态键没有消费者，只会产生一份没人读的真相；多实例/多渠道路由是 v2 的事。
- 若未来需要跨进程观测渠道状态，必须由主线先写契约（键名、字段、TTL、写入者），不许各模块自行发明。

---

## 7. 工具契约

### 7.1 通用约定

- 所有工具用 `@tool` 装饰，docstring 中文、必须写清"无状态/边界/返回体"；
- 返回 `dict` 或 `str`，**失败不抛异常**，统一 `{"ok": false, "error_class": "<稳定字符串>", "message": ..., "hint": ...}`；
- 每个模块导出**固定名字的工厂**，由 `tools.yaml` / agent 装配调用；工厂签名不得随意改；
- 文件工具只接受 `workspace_root` 内绝对路径（resolve 后 `relative_to`）；越界返回 `error_class="path_outside_workspace"`；
- 工具内部不得读取 `.env`、不得打印环境变量、不得把密钥写日志。

### 7.2 Shell（M-SHELL）

文件：`tools/shell.py`、`tools/shell_backend.py`、`tools/winjob.py`、`tools/errors.py`。

```python
# tools/shell_backend.py
@dataclass ShellInfo: name: str; exe: str; version: str      # "pwsh"|"powershell"|"cmd"
def detect_shell() -> ShellInfo                 # 四段：PATH → WindowsApps alias → Program Files → 5.1 → cmd
def build_command(shell: ShellInfo, script: str) -> list[str]  # argv 单层，整段脚本=一个参数

# tools/winjob.py
def create_kill_on_close_job() -> int
def assign_process(job: int, proc: subprocess.Popen) -> None
def terminate_job(job: int) -> None
def close_job(job: int) -> None

# tools/shell.py
def build_shell_tools(*, profile: ToolProfile, workspace_root: Path,
                      tool_outputs_dir: Path, default_cwd: Path) -> list[BaseTool]
```

`Shell(command, cwd=None, timeout_ms=None, shell="auto")` 返回：

```python
{"ok": bool, "exit_code": int|None, "stdout": str, "stderr": str,
 "total_lines": int, "total_bytes": int, "artifact": str|None,
 "duration_ms": int, "shell": "pwsh|powershell|cmd|wsl",
 # 失败时追加：
 "error_class": str, "fingerprint": str, "hint": str,
 "retryable": bool, "attempt": int, "same_fp_count": int}
```

实现要点（**必须照做**）：
1. `subprocess.Popen(argv_list, shell=False, cwd=..., env=minimal_env())`；整段脚本作为单个 argv；
2. 脚本前缀 `try{[Console]::OutputEncoding=[Text.Encoding]::UTF8}catch{};` + `$OutputEncoding=[Text.Encoding]::UTF8;$env:PYTHONUTF8='1';$env:PYTHONIOENCODING='utf-8';`；末尾 `;exit $LASTEXITCODE`；
3. 超时：`TerminateJobObject` 杀整棵树（Job Object `KILL_ON_JOB_CLOSE`），失败兜底 `taskkill /F /T`；
4. router profile 的 `timeout_ms > 30000` 直接拒绝（`error_class="router_long_command"`，提示派 worker）；worker 上限 600000；
5. 超过 2000 行或 64 KB（任一）→ 完整输出写 `tool_outputs/sh_<ulid>.txt`，stdout 只留"头 30 + 尾 60 + 统计 + 绝对路径 + Grep/Read 提示"；不做中间省略；
6. `utils/errors` 的 8 类 `error_class`：`parse_error / command_not_found / permission / encoding / timeout / network / not_found_path / exit_nonzero`；`fingerprint = sha256(shell|规范化命令|error_class|hint_key)[:12]`；
7. WSL：`["wsl.exe","-e","bash","-lc",script]`；输出含 `\x00` 比例高时按 UTF-16LE 解码。

### 7.3 文件工具（M-FS）

文件：`tools/fs.py`、`tools/paths.py`。

```python
def build_fs_tools(*, workspace_root: Path, tool_outputs_dir: Path,
                   max_read_lines: int = 2000) -> list[BaseTool]   # [Read, Write, Edit, DeleteFile, Glob, Grep]
def resolve_in_workspace(path: str | Path, root: Path) -> Path    # 越界抛 ValueError
```

| 工具 | 入参 | 返回 |
|---|---|---|
| `Read` | `path, offset=0, limit=2000, as_="auto"` | 文本：字符串；图片：内容块列表 `[{"type":"text","text":"[图片] <路径>\|mime\|字节"},{"type":"image_url","image_url":{"url":"data:<mime>;base64,..."}}]`（png/jpg/jpeg/gif/webp/bmp，≤4 MB；不缩放；`as_="text"` 时图片按二进制拒绝） |
| `Write` | `path, content` | `{"ok":True,"bytes":n,"path":绝对路径}`，临时文件 + `os.replace` |
| `Edit` | `path, old_string, new_string, replace_all=False` | `{"ok":True,"replacements":n}`；`old_string` 不唯一 → `error_class="not_unique"` |
| `DeleteFile` | `paths: list[str]` | `{"deleted":[...],"failed":[{"path","reason"}]}` |
| `Glob` | `pattern, path=None, limit=200` | `list[str]`（按 mtime 新→旧；排除 `.git/node_modules/.venv/__pycache__`）；空结果返回 `["[no matches] ..."]`；截断时在**列表末尾追加**一条 `"[已截断] ..."` 字符串项 |
| `Grep` | `pattern, path=None, glob=None, n=True, limit=200` | `"文件:行号: 内容"` 多行字符串；跳过二进制与 >2MB；参数名是 `n`（`-n` 不是合法 Python 标识符），语义等价 `grep -n`；空结果/截断同样以文本提示行追加 |

**不新增** `ListDir` / `SearchFiles` 等同义工具。

#### 7.3.1 图片与模型能力（**装配层职责，工具层不判断**）

- `Read` **始终**返回多模态内容块，不查询模型能力、不猜测调用方；
- 由装配层 middleware `VisionAdapterMiddleware` 在每次模型调用前决定：
  - `settings.llm_supports_vision=True` → 原样保留 `image_url` 块；
  - `False` → 把 `image_url` 块替换为等量的 `{"type":"text","text":"[图片已省略：当前模型不支持视觉，原图在工作区 <路径>]"}`，文本块保留；
- 转换只作用于**本次模型请求**，不改 checkpoint 里的 state；后续换成视觉模型仍能读到历史图片；
- 工具层禁止为了让文本模型不报 400 而提前丢弃图片；文本模型收到 image 块的 400 由装配层兜住。

```python
# config.py（冻结）
llm_supports_vision: bool = False          # deepseek-flash v1 为文本模型

# agents/vision.py（主线）
def adapt_content(content, *, supports_vision: bool, placeholder: str = ...) -> Any
def adapt_messages(messages, *, supports_vision: bool, placeholder: str = ...) -> list
```

### 7.4 Router 工具

#### `tools/reply.py`（B1，唯一用户出口）

```python
def build_reply_user_tool(*, channel: Channel, redis_client,
                          ttl_s: int = 86400) -> BaseTool
```

`ReplyUser(text, reply_to=None, quote=False, idempotency_key=None) -> dict`

- `chat_id` 不在入参里：从 runtime context 取 `current_chat_id`（见 §8.4），没有则从 Redis `asst:router:state` 的 `last_chat_id` 取；仍没有返回 `error_class="no_chat_id"`；
- 幂等：`SET NX asst:reply:idem:<trace_id>:<reply_index>`；命中则返回旧 `message_id`；
- 返回 `{"ok":bool,"message_id":str,"error":str}`；
- **全仓库只允许这一个文件 import `channel.send_text`**（worker 无法触达）。

#### `tools/schedule.py`（B1）

```python
def build_schedule_tools(*, store: Store, timezone: str = "Asia/Shanghai") -> list[BaseTool]  # [Schedule]
```

`Schedule(action="create|list|cancel|pause|resume", cron=None, at=None, timezone="Asia/Shanghai", prompt="", misfire_grace_s=300)`

- 返回 `{"schedule_id","next_run_utc","status"}`；`list` 返回数组；
- cron 解析用 `zoneinfo`；存 UTC；`at` 一次性任务执行后 `status="done"`。

#### `tools/profile.py`（B1）

```python
def build_profile_tool(*, profile_path: Path) -> BaseTool
```

`assistant_profile(action="read|write", field=None, value=None)`：读写 `memory/profile.md`（JSON 段落），返回 profile 或 `{"ok":True}`。

#### `tools/skill.py` + `skills/`（B1）

```python
# skills/registry.py
@dataclass SkillCard: name: str; description: str; scope: Literal["router","worker","both"]; path: str
class SkillRegistry:
    def __init__(self, root: Path): ...
    def list_for(self, role: ToolProfile) -> list[SkillCard]
    def load(self, name: str, role: ToolProfile) -> str        # 越 scope → ValueError

# tools/skill.py
def build_skill_tools(*, registry: SkillRegistry, role: ToolProfile) -> list[BaseTool]  # [Skill]
```

`Skill(name=None)`：无 name 返回 `[{"name","description"}]`；有 name 返回正文（字符串）。frontmatter 必含 `name/description/scope`。

#### Router 会话/生命周期工具（`tools/sessions.py`，**主线**）

```python
@dataclass SessionDeps:
    store: Store; bus: Bus; redis_client; settings; repo_root: Path; trace_id: str

class SessionService:
    def create_session(prompt, *, title=None, cwd=None, trace_id=None,
                       task_kind="general", tools_profile="worker") -> dict
    def send_message_to_session(session_id, text, *, wake=True, reply_to=None) -> dict
    def list_sessions(status=None, limit=20, cursor=None) -> list[dict]
    def read_session(session_id, *, cursor=None, limit=20, include_tools=False) -> dict
    def task_output(session_id, *, detail="summary") -> dict
    def stop_command(task_id) -> dict
    def set_session_title(session_id, title) -> dict
    def fork_session(session_id, *, at_message_id=None) -> dict

def build_session_tools(service: SessionService) -> list[BaseTool]
# [create_session, send_message_to_session, list_sessions, read_session,
#  TaskOutput, StopCommand, set_session_title, fork_session]
```

关键返回：

```python
create_session -> {"task_id": ulid, "status": "accepted",
                   "note": "任务已交给 worker，结果会以新消息形式回到上下文；此处没有结果，不要等待。"}
send_message_to_session -> {"ok": True, "queued": True}
TaskOutput -> 未回报：{"status":"no_update","note":"该任务刚派发，还没有任何回报；不要轮询，本轮应当结束或去做别的事"}
              已回报：{"status","pid_alive","last_checkpoint":{...}}
StopCommand -> {"ok": True, "level_reached": 1|2|3}
```

**session_id 归一化**：所有接受 `session_id` 的函数同时接受 `"worker:<ulid>"` 与裸 `<ulid>`；内部统一成 `worker:<ulid>`。

### 7.5 Worker 工具

#### `tools/message.py`（B2）

```python
def build_send_message_tool(*, redis_client, task_id: str, trace_id: str,
                            ask_budget: "QuestionBudget") -> BaseTool
```

`SendMessage(message, kind="notify", timeout_s=30)`：

- `notify` → `XADD asst:stream:router type=worker_notice payload={task_id,kind:"progress",detail:text}`，立即返回 `{"queued":True}`；
- `ask` → `question_id=ulid()`，XADD `worker_message`（带 `deadline_s`），然后 `XREADGROUP asst:stream:worker:<task>` 等 `router_message`，最多 `timeout_s`；超时返回 `"[router 未在 {timeout_s}s 内回复] 请按最保守方案继续，并在结果里说明你的假设"`；
- 预算：每 task ≤20 次、每分钟 ≤6 次（Redis `retry_key` 同款计数或独立 hash 均可），超限返回 `{"ok":False,"error_class":"ask_budget_exhausted"}`；
- 只能发 router，**没有 `to` 参数**。

#### `tools/web.py`（B2）

```python
def build_web_tools(*, api_key: str, base_url: str = "https://api.deepseek.com",
                    tool_outputs_dir: Path, timeout_s: float = 30) -> list[BaseTool]  # [WebSearch, WebFetch]
```

- `WebSearch(query, count=5, site=None, need_content=False)` → `POST /v1/tools/search`，返回 `[{title,url,snippet,summary,date,authority}]`；
- `WebFetch(url, max_chars=20000)` → `POST /v1/tools/fetch`，返回 `{"title","content","truncated"}`；
- 两个端点失败返回 `error_class ∈ {http_error, network, quota}`；长内容按 §7.2 的同一套 stub 规则落盘（复用 `tools/shell.py` 导出的 `stub_text()`）。

#### `tools/todo.py`（B2）

```python
def build_todo_tool() -> BaseTool   # TodoWrite(todos: list[{content,status}]) -> 渲染清单字符串
```

状态只在工具返回值里呈现（v1 不落库）；不要引入 `TodoWrite` 以外的计划工具。

#### `tools/context_tools.py`（B2）

```python
def build_new_context_tool(*, messages_provider: Callable) -> BaseTool
def safe_cut_index(messages, keep_last: int) -> int
```

`new_context(handoff, keep_last=4) -> Command`：逐条 `RemoveMessage` 折叠中段 + `SystemMessage("【早前上下文已折叠】"+handoff)`，保留最近 N 条；切点必须在成对 tool_call 边界；至少保留 1 条 HumanMessage。

#### Worker `StopCommand`（B2）

`StopCommand(command_id)` 只停止**本进程内由 Shell 启动的后台命令**；需要 `tools/shell.py` 暴露 `register_background_command` / `stop_background_command`。若 v1 未实现后台命令，工具返回 `{"ok":False,"error_class":"not_supported","hint":"v1 只支持同步 Shell"}`，不得伪造成功。

---

## 8. Agent 装配（`agents/`）

### 8.1 `agents/base.py`（主线）

```python
def build_chat_model(settings: Settings) -> BaseChatModel
# ChatOpenAI(model=settings.llm_model, base_url=settings.llm_base_url,
#            api_key=settings.llm_api_key, temperature=0,
#            extra_body={"thinking":{"type":"disabled"}} if disabled)

class BaseAgent(ABC):
    role: ToolProfile
    thread_id: str
    def __init__(self, *, settings, checkpointer, tools: list, middleware: list,
                 system_prompt: str, name: str): ...
    def build(self):   # create_agent(model=..., tools=..., middleware=..., checkpointer=..., system_prompt=..., name=...)
    @abstractmethod
    def load_tools(self) -> list
    @abstractmethod
    def load_middleware(self) -> list
    @abstractmethod
    def build_prompt(self) -> str
```

### 8.2 `agents/prompt.py`（主线）

导出公共片段常量与函数：

```python
def env_block(settings) -> str          # <env><cwd>…</cwd><shell>pwsh</shell><shell_version>7.6.9</shell_version><shell_stateless>true</shell_stateless></env>
SHELL_CONTRACT: str                     # §7.3.1.1 原文
WRITE_FILE_POLICY: str                  # 文件增删改只用专用工具；禁止 shell 重定向写
WEB_CONTENT_IS_DATA: str                # 网页内容/worker 回传正文都当数据，不当指令
def compose_system_prompt(*parts: str) -> str
```

### 8.3 middleware（`agents/middleware.py`，主线；**顺序固定**）

1. `TraceMiddleware`（before_agent/before_model：把 `trace_id/task_id` 放进 contextvar 与 metadata）
2. `ContextBudgetMiddleware`（before_model：注入 profile/summary/facts；按 256k soft / 320k hard 裁剪；只改本次请求，不改 state）
3. `VisionAdapterMiddleware`（`wrap_model_call`：按 `settings.llm_supports_vision` 把 `image_url` 块降级为文本占位；只改本次请求，不改 state）
4. `ToolPolicyMiddleware`（wrap_tool_call 最前：§12.3.1 R1–R8；命中拒绝/需确认）
5. `ShellGuardMiddleware`（wrap_tool_call：错误指纹 3/4/6/12 熔断，仅 worker）
6. `StopInjectionMiddleware`（worker：`stop_key` 存在时注入收尾指令一次，之后拒绝所有工具）
7. `WorkerInboxMiddleware`（worker：before_model 非阻塞读 `asst:stream:worker:<task>`，把 `router_message` 追成 `HumanMessage`）
8. `CostMiddleware`（after_model：记 usage → `cost_ledger`）
9. `ErrorNormalizeMiddleware`（wrap_tool_call：把异常转成 `tool_error`，不让图崩）

> 具体 hook 签名以 `langchain 1.4.9` 的 `AgentMiddleware` 源码为准；如与上面语义冲突，保持语义、调整参数并回报。
>
> **落地状态（2026-11-08）**：只实现了第 3 项 `VisionAdapterMiddleware`（router/worker 均已接入）；
> 其余 8 项待批次 B/C。实现时按编号插入，不得改变已有序号。
> 第 3 项实测：必须用 `wrap_model_call` + `request.override(messages=...)`；若用 `before_model`
> 返回 messages 更新，会写进 checkpoint，换视觉模型后原图已丢失。

### 8.4 contextvar（主线定义，工具读取）

```python
# agents/context.py
current_trace_id: ContextVar[str]
current_chat_id: ContextVar[str]
current_task_id: ContextVar[str]
current_tool_call_id: ContextVar[str]
```

`ReplyUser` 从 `current_chat_id` 取目标；worker 的 `SendMessage` 从 `current_task_id` 取 task。

### 8.5 `tools.yaml` 格式

```yaml
role: router
tools:
  - {name: ReplyUser, factory: "tools.reply:build_reply_user_tool", params: {ttl_s: 86400}}
  - {name: create_session, factory: "tools.sessions:build_session_tools", params: {}}
  - {name: Shell, factory: "tools.shell:build_shell_tools", params: {profile: router}}
  - {name: Read, factory: "tools.fs:build_fs_tools", params: {workspace_root: "{workspace_root}"}}
```

规则：`factory` 是 `模块:函数`；函数可以返回单个工具或列表；`params` 只允许传配置值，不允许传 lambda/对象。`{workspace_root}` / `{tool_outputs_dir}` 由装配器替换。

### 8.6 `agents/router/graph.py`（B1）

```python
class RouterAgent(BaseAgent):
    role = "router"; thread_id = "router:main"
def build_router(settings, checkpointer, deps: SessionDeps) -> CompiledGraph
```

### 8.7 `agents/worker/graph.py`（B2）

```python
class WorkerAgent(BaseAgent):
    def __init__(self, *, task: TaskSpec, settings, checkpointer, redis_client): ...
    role = "worker"; thread_id = f"worker:{task.task_id}"
def build_worker(task: TaskSpec, settings, checkpointer, redis_client) -> CompiledGraph
```

---

## 9. 生命周期语义（主线实现）

### 9.1 router 一轮

```
启动: ensure group → 起 heartbeat 线程（TTL 10s，每 5s 续期）
      XAUTOCLAIM(5s 空闲) 认领上次崩溃遗留 → loop:
  batch = read_batch(block=router_read_block_ms)
  if empty: 有 PEL → claim_pending(5s)；仍空且 lag==0 且 idle≥60s → 删 heartbeat、退出 0
  batch = drain_more(batch, debounce=200ms, max=10)
  写 inbox-*.jsonl（每条事件一行）
  worker_result 事件：更新 sessions.status / first_message_at（router 的职责，worker 不写业务表）
  chat_id/chat_type/trace_id 从本批用户消息取 → contextvar + asst:router:state.last_chat_id
  graph.invoke({"messages":[{"role":"user","content":render_digest(batch)}]},
               config={thread_id:"router:main", recursion_limit=30})
  写 turns-*.jsonl（append+fsync）→ ACK
```

- 失败**不 ACK**，进程以退出码 2 退出；事件留在 PEL，下次启动重放（没有逐事件重试预算，
  由 gateway 的 supervisor 负责有限次重试，见 9.3）；
- `ReplyUser` 幂等键 `trace_id + ":" + reply_index`；
- 退出前必须满足 `lag()==0`，否则可能漏掉刚到达、尚未投递的事件（已实测）。

### 9.2 worker 生命周期

- `create_session`：生成 `task_id=ulid()`，写 `workers/<task_id>/task.json`（`TaskSpec`），`Popen([python, worker_runner.py, "--task", task_id], creationflags=CREATE_NEW_PROCESS_GROUP, stdout=logs/worker-<id>.log)`，记录 `(pid, pid_started_at)`；返回 `accepted`；
- `pid_started_at`：用 `GetProcessTimes`（ctypes）或 `psutil`（若已装）取创建时间，UTC ISO；
- `TaskOutput`：`first_message_at IS NULL` → `no_update`；否则读最新 checkpoint（**只读**），`pid_alive` 用 `(pid, pid_started_at)` 二元组（±2s）；
- `StopCommand`：一级不打断当前工具；一级返回后写 `stop_key=1`；二级 60–90s 未收尾 → `stop_key=2` + `TerminateJobObject`/`taskkill /F /T`，并写用户可见 `task_abort` 记录（`ReplyUser` 或 `approval_log`）；
- 巡检 `reason ∈ {stalled, process_gone}`；`UNIQUE(task_id, reason)` 去重；**绝不自动重派**。
- **失败也必须回传**：`worker_runner.py` 无论成功失败都 `XADD worker_result`（失败时 `status="error"`，正文给失败层与原因），并把结果落盘到 `workers/<task_id>/result.json`；否则 router 永远收不到回报。
- 子进程一律用 `python -u` 启动：否则崩溃现场会因 stdout 缓冲整段丢失（已实测）。

### 9.3 supervisor

```python
def supervisor_loop(settings, bus, store, stop_event)   # 1s 心跳/待处理；5s schedules；300s watch
```

只 `XADD` 事件，不 import LLM、不执行任务。

已实现的 router supervisor（gateway 内线程）：

```
每 1s:
  回收已退出的 router 子进程（正常 0 / 异常非 0 分别计数）
  若 有未投递(lag>0) 或 PEL 非空，且 heartbeat 不存在，且抢到 asst:router:spawning 锁 → Popen(python -u main_router.py)
  异常连续 ≥5 次 → 停止拉起（多半是环境/凭证问题，继续拉只会刷日志）
```

- 拉起条件**不能用 `XLEN`**（见 §4）；
- 命令行 `--console` 必须透传给 router，保证收发用同一种渠道。

---

## 10. 文件归属表

> "冻结"= 只有主线可改。所有路径相对仓库根。

| 文件 | 模块 | 依赖 | 冻结 | 验收 |
|---|---|---|---|---|
| `contracts.py` | M0 主线 | 标准库 | ✅ | `python -c "import contracts"` |
| `config.py` | M0 主线 | pydantic-settings | ✅ | `python -c "from config import settings"` |
| `bus/envelope.py` | M0 主线 | contracts | ✅ | `scripts/check_contracts.py` |
| `bus/stream.py` | M0 主线（M-STORE 实现，已验收） | redis, envelope | ❌ | `scripts/check_store.py` |
| `channel/base.py` | M0 主线 | contracts | ✅ | import |
| `agents/context.py` | M0 主线 | contextvars | ✅ | import |
| `agents/vision.py` | M0 主线 | 标准库（duck typing message） | ✅ | `scripts/check_contracts.py` |
| `agents/base.py` | M0 主线 | langchain, config | ❌ | `scripts/check_agents.py` |
| `agents/middleware.py` | M0 主线 | langchain, agents.vision, store | ❌ | 同上 |
| `agents/prompt.py` | M0 主线 | config | ❌ | 同上 |
| `main_gateway.py` | M0 主线 | channel, bus, store | ❌ | `scripts/selfcheck.py` |
| `main_router.py` | M0 主线 | agents.router, bus, store | ❌ | 端到端 |
| `worker_runner.py` | M0 主线 | agents.worker, bus, store | ❌ | 端到端 |
| `tools/sessions.py` | M0 主线（生命周期） | store, bus, worker_runner | ❌ | `scripts/check_sessions.py` |
| `scripts/selfcheck.py` | M0 主线 | 全部 | ❌ | 退出码 0 |
| `docs/ENGINEERING_CONTRACT.md` | M0 主线 | — | ✅ | — |
| `tools/shell.py` | M-SHELL | contracts, config | ❌ | `scripts/check_shell.py` |
| `tools/shell_backend.py` | M-SHELL | 标准库 | ❌ | 同上 |
| `tools/winjob.py` | M-SHELL | ctypes | ❌ | 同上 |
| `tools/errors.py` | M-SHELL | 标准库 | ❌ | 同上 |
| `scripts/check_shell.py` | M-SHELL | — | ❌ | 退出码 0 |
| `tools/fs.py` | M-FS | contracts | ❌ | `scripts/check_fs.py` |
| `tools/paths.py` | M-FS | 标准库 | ❌ | 同上 |
| `scripts/check_fs.py` | M-FS | — | ❌ | 退出码 0 |
| `store/sqlite.py` | M-STORE | contracts | ❌ | `scripts/check_store.py` |
| `store/checkpointer.py` | M-STORE | psycopg, langgraph | ❌ | 同上 |
| `memory/jsonl.py` | M-STORE | contracts | ❌ | 同上 |
| `scripts/check_store.py` | M-STORE | — | ❌ | 退出码 0 |
| `channel/feishu.py` | M-CHANNEL | lark-oapi, channel.base | ❌ | `scripts/check_channel.py` |
| `channel/console.py` | M-CHANNEL | channel.base | ❌ | 同上 |
| `channel/factory.py` | M0 主线 | channel.*, config | ❌ | `scripts/check_contracts.py` |
| `scripts/check_channel.py` | M-CHANNEL | — | ❌ | 退出码 0 |
| `tools/__init__.py` | M0 主线 | — | ❌ | `python -c "import tools"` |
| `tools/reply.py` | M-ROUTER | channel.base, redis | ❌ | `scripts/check_router_tools.py` |
| `tools/schedule.py` | M-ROUTER | store | ❌ | 同上 |
| `tools/profile.py` | M-ROUTER | contracts | ❌ | 同上 |
| `tools/skill.py` | M-ROUTER | skills.registry | ❌ | 同上 |
| `skills/registry.py` / `skills/loader.py` | M-ROUTER | 标准库 | ❌ | 同上 |
| `skills/shared/**`、`skills/router-only/**` | M-ROUTER | — | ❌ | — |
| `agents/router/graph.py` / `prompt.py` / `tools.yaml` | M-ROUTER | agents.base, 上列工具 | ❌ | `scripts/check_router_agent.py` |
| `tools/message.py` | M-WORKER | redis, envelope | ❌ | `scripts/check_worker_tools.py` |
| `tools/web.py` | M-WORKER | httpx | ❌ | 同上 |
| `tools/todo.py` | M-WORKER | langchain | ❌ | 同上 |
| `tools/context_tools.py` | M-WORKER | langchain, langgraph | ❌ | 同上 |
| `agents/worker/graph.py` / `prompt.py` / `tools.yaml` | M-WORKER | agents.base, 上列工具 | ❌ | `scripts/check_worker_agent.py` |
| `memory/context.py` / `memory/consolidate.py` | M-MEMORY | store, memory.jsonl | ❌ | `scripts/check_memory.py` |

---

## 11. 共享文件与变更流程

**冻结文件（只允许主线改）**

```
contracts.py
config.py
bus/envelope.py
channel/base.py
agents/context.py
agents/vision.py
docs/ENGINEERING_CONTRACT.md
requirements.txt
.env.example
.gitignore
```

**规则**
1. 并行分支**禁止**直接编辑上述文件；需要新字段/新常量时，在报告里写"变更请求：文件 / 字段 / 类型 / 默认值 / 影响模块"。
2. 主线改完后在 `main` 提交，附一条 `contract:` commit；各分支 `git rebase main` 后再继续。
3. 任何模块不得新增"同义共享结构"（例如自建 `Event` / `SessionRow`）；一律 import `contracts.py`。
4. 模块之间只允许 import 对方**公开工厂/类**（本契约列出的符号）；不得 import `_private` 函数或读取对方内部文件。

---

## 12. 模块切片与并行批次

### 批次 A（可立即并行，彼此零依赖）

| 模块 | 工作树目录 | 分支 | 交付物 | 验收命令 |
|---|---|---|---|---|
| **M-SHELL** | `personal-assistant-dev-shell` | `feat/shell` | `tools/shell.py`、`tools/shell_backend.py`、`tools/winjob.py`、`tools/errors.py`、`scripts/check_shell.py` | `python scripts/check_shell.py` |
| **M-FS** | `personal-assistant-dev-fs` | `feat/fs` | `tools/fs.py`、`tools/paths.py`、`scripts/check_fs.py` | `python scripts/check_fs.py` |
| **M-STORE** | `personal-assistant-dev-store` | `feat/store` | `store/sqlite.py`、`store/checkpointer.py`、`memory/jsonl.py`、`scripts/check_store.py` | `python scripts/check_store.py` |
| **M-CHANNEL** | `personal-assistant-dev-channel` | `feat/channel` | `channel/feishu.py`、`channel/console.py`、`scripts/check_channel.py` | `python scripts/check_channel.py` |

共同依赖：`contracts.py` / `config.py` / `bus/envelope.py`（已冻结，无需等待其他模块）。  
**M-STORE 的 Postgres 用例**：若本机 PG 未起，脚本允许跳过并发测试，但 SQLite/JSONL 必须全过；跳过需打印 `SKIP(postgres)`。

### 批次 B（依赖 A 的公开接口，可并行）

| 模块 | 工作树目录 | 分支 | 交付物 | 依赖 |
|---|---|---|---|---|
| **M-ROUTER** | `personal-assistant-dev-router` | `feat/router` | `tools/reply.py`、`schedule.py`、`profile.py`、`skill.py`、`skills/**`、`agents/router/**` | M-STORE 的 `Store`/`JsonlWriter`、M-CHANNEL 的 `Channel` |
| **M-WORKER** | `personal-assistant-dev-worker` | `feat/worker` | `tools/message.py`、`web.py`、`todo.py`、`context_tools.py`、`agents/worker/**` | M-SHELL 的 `stub_text`/错误结构（可先按契约写，再 rebase） |
| **M-MEMORY** | `personal-assistant-dev-memory` | `feat/memory` | `memory/context.py`、`memory/consolidate.py` | M-STORE 的 `JsonlWriter` |

### 批次 C（主线，不建议并行）

| 模块 | 交付物 | 说明 |
|---|---|---|
| **M-LIFECYCLE** | `tools/sessions.py`、`main_gateway.py`、`main_router.py`、`worker_runner.py` | spawn/杀进程/巡检/超时耦合最多，必须留主线 |
| **M-AGENTS-BASE** | `agents/base.py`、`middleware.py`、`prompt.py`、`agents/router/graph.py` 的装配对接 | 依赖 M-ROUTER/M-WORKER 的工厂 |
| **M-INTEGRATION** | `scripts/selfcheck.py`、端到端联调 | 最后做 |

**批次建议**：A 四路并行 → 合并验收 → B 三路并行 → 合并 → C 主线闭环。  
若人手不足，优先顺序：M-STORE → M-SHELL → M-CHANNEL → M-FS → M-WORKER → M-ROUTER。

---

## 13. 验收矩阵（主线合并前必须全绿）

| # | 用例 | 期望 |
|---|---|---|
| 1 | `check_shell`：`pwsh` 探测、`echo`、`$LASTEXITCODE` 透传、中文输出、超时杀进程树、长输出落盘 | 全过 |
| 2 | `check_fs`：写/读/改/删/Glob/Grep；图片返回 data URI 内容块；`../` 越界被拒 | 全过（M-FS 实测 56/56） |
| 3 | `check_store`：建表、session 生命周期、schedule due、watch 去重、JSONL seq+fsync、Redis PEL 崩溃重放、PG checkpoint 往返 | 全过（M-STORE 交付，脚本 0 failure） |
| 4 | `check_channel`：console 收发；飞书事件解析 fixture | 全过；真实飞书发送由 `FEISHU_LIVE=1` 门控 |
| 5 | `check_router_tools`：ReplyUser 幂等（同 key 只发一次）、Schedule 创建/取消、Skill 清单 | 全过 |
| 6 | `check_worker_tools`：SendMessage notify 立即返回；ask 超时文案；Web 失败结构化 | 全过 |
| 7 | `check_memory`：预算裁剪保留最后 HumanMessage、tool 成对；折叠切点安全 | 全过 |
| 8 | `scripts/selfcheck.py`：Redis group、PG checkpoint 读写、SQLite 表、工具路径守卫 | 退出码 0 |
| 9 | `scripts/check_contracts.py`：`adapt_messages` 在 `llm_supports_vision=False` 时把 image 块换成文本、文本块保留；`True` 时原样返回 | 全过 |
| 10 | 端到端：手机发"你好" → router 回 ReplyUser；派"看下 workspace 有什么" → worker_result → router 再回一次 | 未完成：缺 LLM 凭证（见下方实测记录） |

**端到端实测记录（2026-11-08，console 模式）**

- ✅ `gateway --send` → 入队 → supervisor 拉起 router 子进程 → 消费者组读到消息（`lag` 归零）
- ✅ router 装配成功（`create_agent` + 9 个工具：ReplyUser + 8 个 session 工具）
- ✅ worker 真实拉起：`create_session` → `worker_runner.py` → 读到 `task.json` → 失败仍 `XADD worker_result(status="error")` → sessions 行转为 `error` 且写入 `first_message_at`
- ✅ gateway 对异常退出按退避策略重拉，连续 5 次后停止；`Ctrl-C`/超时停止子进程无残留
- ❌ **阻塞层：LLM 凭证缺失**。当前 `.env` 的 `LLM_API_KEY` / `LLM_BASE_URL` 为空，模型调用返回 `401`（`https://api.openai.com/v1/chat/completions`），router 因此无法产生任何工具调用，闭环停在"模型层"。
  该层一旦可用，`ReplyUser → channel.send_text` 是唯一未跑的路径（其余链路均已实测通）。

---

## 14. 文档冲突与实现取舍（已决定，实现按此执行）

| # | 冲突/事实 | 取舍 |
|---|---|---|
| 1 | 需求给的骨架路径 `temp_20261108_03\feishu_langgraph_skeleton` **不存在**；实际在 `temp_20261108_02\feishu_langgraph_skeleton`（PID 23680 正在跑） | 以 `_02` 为只读参考；**不复用其代码目录、不改动它** |
| 2 | 设计文档 §7.1 写"本机没有 pwsh 7 / `Get-Command pwsh` False"；实测 `$PSVersionTable` = 7.6.9，alias 在 `%LOCALAPPDATA%\Microsoft\WindowsApps` | 以实测为准，默认 `pwsh`；四段探测仍全部实现 |
| 3 | 设计文档建议 `types.py`；但仓库根 `types.py` 会遮蔽标准库 `types` | 改名 `contracts.py` |
| 4 | worker 侧 `StopCommand` 入参在 §6.2 写 `command_id`，router 侧写 `task_id` | 同名不同签名：router `StopCommand(task_id)`，worker `StopCommand(command_id)`；由不同工厂注入 |
| 5 | `SendMessage` 超时参数 §5.6.4 用 `timeout_s`，§6.2 也用 `timeout_s`；但 Shell 用 `timeout_ms` | 统一：`SendMessage.timeout_s`（秒），`Shell.timeout_ms`（毫秒） |
| 6 | Shell 返回体 §6.2 有 `truncated`，§7.3.4 用 `artifact/total_lines/total_bytes` | 以 §7.3.4 为准；额外保留 `truncated: bool` 兼容旧描述 |
| 7 | `sessions.status` 文档多处枚举不完全一致 | 统一为 `running/waiting_router/idle/stopped/error/done/unreachable`（`contracts.SessionStatus`） |
| 8 | `TaskOutput` 有 `session_id`，`StopCommand` 有 `task_id` | 所有 session 工具内部接受两种写法并归一化为 `worker:<ulid>` |
| 9 | §10.2 思考模式 + 工具调用可能 400（未验证） | v1 默认 `LLM_THINKING_DISABLED=true`；真 key 实验后再改默认值 |
| 10 | `ReplyUser` 需要 `chat_id`，但工具签名没有 | 由 runtime contextvar `current_chat_id` 提供；gateway 每批把最后一条用户消息的 chat_id 写 `asst:router:state` 兜底 |
| 11 | 设计文档 §6.2 router 工具含 `ToolSearch/DeferExecuteTool` 仅服务 `fork_session` | v1 注册这两个工具但只允许 `fork_session`；`fork_session` 用 `checkpointer.get_tuple + put` 复制 thread |
| 12 | `langchain` 版本文档未定 | 实测 `langchain 1.4.9` + core 1.7.6 可 `from langchain.agents import create_agent`，锁定该版本 |
| 13 | 当前 env 缺 `langchain` | 已补装（1.4.9）；`requirements.txt` 已锁 |
| 14 | worker 巡检的 `beat_at` 取自 `checkpoints.created_at` | 表由 `PostgresSaver.setup()` 建；只读 `MAX(created_at)`，失败只记日志不阻断 |
| 15 | 设计文档 §7.3 写 `Read` 图片返回 `image_not_supported`；用户明确要求 `Read` 支持读图 | **以用户要求为准**：工具返回 `image_url` data URI 内容块（M-FS 实现，验收 56/56）；不把实现改回去 |
| 16 | 设计文档建议 `Grep(-n=True)`，但 `-n` 是非法 Python 标识符 | 参数名统一为 `n=True`，语义等价 `grep -n` |
| 17 | `Glob` 契约写 `list[str]`，但空结果/截断需要提示 | 保持不变：提示以额外字符串项追加到列表末尾，不改成 dict |
| 18 | 文本模型收到 image 块会 400 | 装配层 `VisionAdapterMiddleware` 按 `llm_supports_vision` 降级；工具层不判断模型能力 |
| 19 | M-STORE 新增公开辅助函数 `try_dedupe` / `next_run_utc_after` | 接受：都是新增符号，不改既有签名；已写入 §4.3 / §5.2 |
| 20 | cron 只支持标准 5 段数字语法 | 接受（v1 限制）：不支持 `JAN`/`L`/`W`；非法表达式抛 `ValueError` |
| 21 | M-STORE 验收留下 Redis 空 stream + 组 `router`、PG schema `mstore_check` | 保留：生产 `public` 已核对未变；组可在 selfcheck 复用，`mstore_check` 不参与生产 |
| 22 | `read_records` 按目录读取全部 `*.jsonl`（不按前缀过滤） | 接受：router 的 memory 目录只放 turns/inbox/tools 三类，读取方自行按字段过滤 |
| 23 | `tools/` 目录此前没有 `__init__.py`（不在归属表），以命名空间包工作 | **补 `__init__.py`，按显式包管理**；与 `bus/`、`channel/`、`agents/` 统一；文件归 M0 |
| 24 | 契约未定义 channel→Redis 的"状态上报"键 | **v1 不建键**（见 §6.5）；健康信息只走结构化日志 + `SendResult`；未来要建键必须主线先写契约 |
| 25 | `EVENT_SOURCES` 原本没有 `console`，而 `ConsoleChannel` 就是合法来源 | 已加入 `{"console"}`；`source` 校验放宽为 6 值，业务语义不变 |
| 26 | router / gateway 都需要"选渠道"，契约没有工厂 | 新增 `channel/factory.py`（§6.4）；**必须惰性导入 feishu**：`import lark_oapi` 实测 47.2s |
| 27 | 子进程 stdout 重定向到文件时，崩溃日志会因缓冲丢失 | 所有子进程统一 `python -u` 启动（router 由 supervisor 拉起、worker 由 `create_session` 拉起） |
| 28 | 用 `XLEN` 判断"是否有活"会把 router 反复拉起（XACK 不删条目） | supervisor 改用 `XGROUP lag` + `pel_nonempty`（`Bus.lag()`，§4.3） |
| 29 | 设计文档 §9.1 写"失败后 sleep 1s 重放" | 实现为**进程退出码 2 + PEL 保留**，由 supervisor 有限次（5 次、指数退避）重拉；进程内不 sleep 重放 |
| 30 | 设计文档写 worker 完成后回传结果，未规定失败路径 | 失败也必须 `XADD worker_result(status="error")`，否则 router 永远等不到回报（§9.2） |
| 31 | `Thread` 子类把停机事件命名为 `_stop` 会遮蔽 `threading.Thread._stop()`，`join()` 抛 `TypeError` | 统一命名 `_stop_event`（已实测踩坑，写进契约防复发） |
| 32 | `ReplyUser` 工厂函数漏 `return`，导致工具列表里出现 `None`，`create_agent` 报 `'function' object has no attribute 'name'` | 已修；`build_*_tools()` 必须有返回值，工具对象类型断言留给 `check_router_tools` |

---

## 15. Git / Worktree 约定

1. 仓库：`C:\Users\34897\Documents\Trae-CN\temp_20261108_06\personal-assistant`（main = 基线，**不允许直接开发**）。
2. 每个模块一个 worktree：`..\personal-assistant-dev-<module>`，分支 `feat/<module>`，从 `main` 切出。
3. 提交信息：`feat(<module>): <一句话>`；契约变更：`contract: <字段/常量>`。
4. 合并流程：模块自测通过 → 在分支提交 → 报告"模块名 + 分支 + 验收输出" → 主线 `git merge --no-ff` → 跑 `scripts/selfcheck.py`。
5. 不提交 `.env`、日志、运行期数据、`__pycache__`。
6. 同一模块内不要跨 worktree 复制未合并代码；需要其他模块接口时，按契约写 import，等对方合并后 rebase。

---

## 16. 交付报告模板（每个并行会话只需回这一段）

```
模块：M-XXX
分支/worktree：feat/xxx @ personal-assistant-dev-xxx
交付文件：<列表>
验收命令与结果：python scripts/check_xxx.py → PASS/FAIL（附最后 5 行）
契约变更请求：无 / <文件+字段+原因>
已知限制：<如实写；没有就写"无">
```
