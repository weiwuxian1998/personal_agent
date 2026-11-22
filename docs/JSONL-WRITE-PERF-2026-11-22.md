# JsonlWriter.append 性能调查与实施（2026-11-22）

> 状态：**已实施方案 C**（反向读文件尾部取 max(seq)）。§1–§7 是最初的调查与方案对比，
> §8 是落地记录与实施后的实测数据。

## 1. 结论摘要

`JsonlWriter.append()` 每次调用都会执行 `next_seq()`，后者把**目录下该前缀的所有历史文件**
逐行 `json.loads` 扫一遍求 `max(seq)`。于是：

- 单次 append 成本 = `O(历史全部行数 × 单行大小)`，与"今天写了多少"无关；
- 成本**只增不减**：文件按天新增、从不删除，扫描范围每天都在变大；
- 实测一年量级（83.9 MB / 7300 行）时单次 append **1.5 秒**，而 transcript 每收到一条消息
  就要 append 一次（一条用户消息通常触发 2–10 次）——每轮对话会凭空多出数秒到十几秒延迟。

成本结构（27000 行 / 12.6 MB 实测）：`next_seq()` 全量扫描解析 **≈184 ms（98%）**，
纯 `open + write + fsync` 仅 **1.5 ms**。所以问题不在 fsync，也不在磁盘。

## 2. 调用链与写者模型

```
TranscriptMiddleware._stamp_and_journal  → JsonlWriter(transcript_dir,"transcript").append()   每条新消息一次
main_router._process_batch               → JsonlWriter(ledger_dir,"inbox").append()            每批事件一条
main_router 写 turn                      → JsonlWriter(ledger_dir,"turns").append()            每轮一条
memory/ledger.append_consolidated        → JsonlWriter(ledger_dir,"turns").append()            每次整理完成一条
```

- **写者只有一个进程**：全部在 router 进程内（`main_gateway.py` 不写任何 JSONL）。
- 但 `memory/ledger.py:237/260` 每次调用都 `JsonlWriter(...)` **新建实例**，
  所以"实例级缓存"这条路天然失效，缓存必须放在模块级或类级。
- `seq` 的消费方：`read_records(since_seq/until_seq)`、`last_consolidated_seq`、
  `count_turns_after` —— 只要求**单调递增**，不要求连续。

## 3. 实测数据

环境：Windows / Python 3.11（conda `feishu`）/ 本机 SSD；每档取 3–15 次均值。

### 3.1 现网真实数据（复制到临时目录测，未污染生产）

| 目录 | 规模 | append 均值 | 峰值 |
|---|---|---|---|
| `router/ledger`（turns，25 个文件） | 632 行 / 0.74 MB | **29.0 ms** | 112.8 ms |
| `router/transcript`（单文件） | 305 行 / 0.42 MB | **13.8 ms** | 14.9 ms |

> turns 行数少但每行大（平均 ~12 KB，含完整对话正文），所以按行数看不算慢，按字节看已经慢了。

### 3.2 合成规模（每行 400 B）

| 天数 × 每天行数 | 总行数 / 体积 | append 均值 | 峰值 | 尾部读 seq |
|---|---|---|---|---|
| 1 × 100 | 100 行 / 0.04 MB | 10.4 ms | 12.9 ms | — |
| 10 × 100 | 1 000 行 / 0.5 MB | 21.2 ms | 105 ms | — |
| 30 × 100 | 3 000 行 / 1.4 MB | 45.9 ms | 310 ms | — |
| 90 × 100 | 9 000 行 / 4.2 MB | 120.5 ms | 923 ms | — |
| 180 × 300 | 54 000 行 / 25.2 MB | 340.1 ms | 2 035 ms | — |
| 365 × 300 | 109 500 行 / 51.2 MB | 670.0 ms | 4 053 ms | — |

### 3.3 合成规模（每行 12 KB，贴近真实 turns 大小）

| 天数 × 每天行数 | 总行数 / 体积 | append 均值 | 尾部读 seq |
|---|---|---|---|
| 30 × 20 | 600 行 / 6.9 MB | 128.0 ms | 2.6 ms |
| 180 × 20 | 3 600 行 / 41.4 MB | 767.0 ms | 3.7 ms |
| 365 × 20 | 7 300 行 / 83.9 MB | **1 507 ms** | 3.8 ms |

### 3.4 成本分解（27000 行 / 12.6 MB）

| 分段 | 耗时 |
|---|---|
| `_files()` glob | 1.48 ms |
| `next_seq()` 全扫解析 | **184.47 ms** |
| `append()` 总计 | 107.64 ms（两次测量受页缓存影响，量级一致） |
| 纯 `open + write + fsync` | 1.47 ms |
| 纯 `open + write`（无 fsync） | 0.77 ms |
| 反向读文件尾部取 seq | 2.46 ms |

## 4. 影响评估

- **现在**：29 ms/次。一条用户消息触发 2–10 次 transcript append，感知不到，但已经是纯浪费。
- **3 个月后**（turns ≈ 20 MB）：单次 ~300–400 ms；每轮对话多 0.6–4 s。
- **1 年后**（turns ≈ 80 MB）：单次 1.5 s；每轮多 3–15 s，且 `next_seq` 在 `before_model` 钩子里，
  会**阻塞模型调用前的准备**，表现为"发消息半天不动"。
- 另外 `next_seq` 有 O(n²) 的总量特征：一天写入 n 条，当天累计解析 n²/2 行。

## 5. 方案对比

| 方案 | 改动量 | 单次成本 | 跨进程/重启安全 | 主要风险 |
|---|---|---|---|---|
| **A. 模块级缓存 last_seq** | ~5 行 | O(1) | 单进程 OK；重启首次全扫 | 多写者会撞 seq；`ledger.py` 每次新建实例，缓存必须放模块级 |
| **B. sidecar 游标文件**（`.*.seq`） | ~15 行 + 自愈 | O(1) | 较好 | 多一个文件；崩溃后游标与实际不一致，需 max(游标, 尾部) 自愈 |
| **C. 反向读文件尾部取 max(seq)** | ~20 行 | **O(1)（2–4 ms）** | 无状态，天然跨进程/重启 | 依赖"永不回写旧记录"（契约已明确）；尾行损坏需前向回退 |
| **D. 历史归档到 `archive/` 子目录** | 0 行 + 运维 | 不变（但扫描范围受限） | — | `read_records` 只 glob `*.jsonl`，会把归档历史读丢；需同步改读取方 |
| **E. 降频/去掉 fsync** | ~3 行 | 省 0.7 ms | — | 收益可忽略（占 1%），且违背契约 §5.3 逐条 fsync |

## 6. 推荐：方案 C（读尾部）

理由：无状态、不需要额外的游标文件、天然跨进程安全、崩溃后可回退、成本恒定 2–4 ms，
而且不需要改任何调用方和 `read_records` 语义。

实现要点（伪代码，未落地）：

```python
def next_seq(self) -> int:
    # seq 全局单调且只追加，所以"最后一行"就是全局最大值。
    candidates = [self.current_path()]
    candidates += [f for f in reversed(self._files()) if f != candidates[0]][:1]
    for path in candidates:
        if not path.exists() or path.stat().st_size == 0:
            continue
        seq = _tail_max_seq(path)      # 反向 8 KB 块读，逐行 json.loads，跳过坏行
        if seq is not None:
            return seq + 1
    return 1                           # 目录为空

def _tail_max_seq(path: Path, chunk: int = 8192) -> int | None:
    with path.open("rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        while size > 0:
            step = min(chunk, size)
            size -= step
            fh.seek(size)
            for line in reversed(fh.read(step).splitlines()):
                try:
                    seq = json.loads(line).get("seq")
                except Exception:
                    continue           # 崩溃留下的半行：跳过
                if isinstance(seq, int) and seq > 0:
                    return seq
            if size == 0:
                break
    return None
```

边界处理：

1. **当天第一个文件**（`current_path()` 不存在）→ 回退读最新一个旧文件尾部；
2. **最后一行是半行**（崩溃）→ 跳过坏行继续往前，与现有 `next_seq` "跳过坏行" 语义一致；
3. **整个尾部块都是坏行** → 逐块向前，最多把文件读完（退化成本次全扫，仅一次）；
4. **目录为空** → 返回 1。

验证方式（改完后）：保留现有检查，补 3 条断言 ——
`seq` 跨天连续、崩溃半行后仍能取到正确 seq、`append` 后 `read_records` 能按 seq 过滤到新行；
再跑一遍本文件的基准脚本对比数字。

## 7. 附：本次没有改动的部分

- `JsonlWriter` 实现未动（§8 已改）；
- 未做历史归档（方案 D），因为会改变 `read_records` 的可见范围，需要单独评估；
- 未调整 fsync（方案 E），收益 1%，不值得冒契约风险。

---

## 8. 实施记录（方案 C 已落地）

### 8.1 代码改动

全部集中在 `memory/jsonl.py`，未动调用方与 `read_records` 语义。

| 位置 | 改动 |
|---|---|
| `_tail_seq(path)` | 新增。从文件末尾读 8 KB，反向逐行 `json.loads`；窗口内无有效行则翻 4 倍扩窗，上限 1 MB。窗口起点 >0 时丢弃首段（必为截断残片）。用 `os.fstat` 取大小，省掉一次 `stat` |
| `_names_desc()` | 新增，替代原 `_files()`。用 `os.scandir` + 字符串排序代替 `glob` + `sorted(Path)` |
| `next_seq()` | 重写：探 `current_path()` 与日期最新文件尾部，取 max + 1；两者都无有效记录时按日期倒序回退 |

三处与最初伪代码的偏差（验证中发现的，不是拍脑袋加的）：

1. **`glob` 换 `scandir`**。profile 显示 365 个文件时 `glob + sorted(Path)` 独占 3.1 ms/次
   （每项都要 fnmatch + 构造 Path + 比较），换成 `scandir` + 字符串比较后降到 ~0.2 ms。
   文件名 `turns-YYYY-MM-DD.jsonl` 的字典序与日期序一致，可以直接字符串比大小。
2. **`current_path()` 与"日期最新文件"都要探**。只读"排序最后一个文件"时会漏一个边界：
   若目录里存在**日期晚于今天**的文件（时钟回拨、手工拷入），`append` 写的是今天文件，
   而全局最大 seq 在那份未来文件里 —— 实测会让 seq **卡住不动**（连续 10 次 append 都返回 7301）。
   旧实现取全局 max，本来是对的，不能因为改性能把语义改坏。现在两者取 max。
3. **`_tail_seq` 内 `open` 后 `os.fstat`**，不用先 `stat` 再 `open`（少一次 syscall）。

### 8.2 实施后实测

环境同上（Windows / Python 3.11 / 本机 SSD），`next_seq` 取 30 次中位数。

| 规模 | 实施前 append | 实施后 `next_seq` | 实施后 append |
|---|---|---|---|
| 20 行 / 0.2 MB | 10.4 ms | **0.42 ms** | 11.3 ms |
| 600 行 / 6.9 MB | 45.9 ms | **0.51 ms** | 10.9 ms |
| 7 300 行 / 83.9 MB（365 天） | **1 507 ms** | **0.85 ms** | 11.1 ms |
| 36 500 行 / 419.4 MB | 未测（O(n) 外推 ~7 s） | **1.22 ms** | 10.9 ms |

`next_seq` 现在与历史体量基本脱钩：0.2 MB → 419 MB（2000 倍）只从 0.42 ms 涨到 1.22 ms，
增量来自 `scandir` 的目录项数量（365 个文件名），不是数据量。

**现网数据对账**（复制到临时目录，未污染生产）：`router/ledger` 12 个文件 285 行，
新旧都返回 286；`router/transcript` 319 行，新旧都返回 320；两处 seq 全局单调性校验通过。

### 8.3 剩下的 10 ms 不是算法成本

实施后 `append()` 仍约 **10.9 ms**，而同一时刻单独测 `next_seq()` 只有 **1.2 ms**。
逐段插桩（419 MB 场景，中位数）：

| 分段 | 耗时 |
|---|---|
| `next_seq()`（在 append 循环内） | 8.42 ms |
| `json.dumps` | 0.05 ms |
| `open + write + flush` | 0.98 ms |
| `os.fsync` | 0.55 ms |

同一个 `next_seq()`，脱离写循环只要 1.2 ms，放进写循环变 8.4 ms —— 说明瓶颈在
"**刚写过这个文件，立刻再读它**"。单独验证：

| 场景 | 读 8 KB 耗时 |
|---|---|
| `fsync` 后立刻读**同一文件** | **8.35 ms** |
| 同时读一个无关文件 | 0.41 ms |
| 只 `flush` 不 `fsync` 后读同一文件 | 7.75 ms |

所以这 8 ms 是 Windows 上"写后读一致性"的固有开销（NTFS cache manager 刷写 /
杀软实时扫描都可能参与），**与扫尾算法无关，旧实现同样在付**——只是被全扫的 1.4 s 盖住了。

要让 append 再降到 2 ms 量级，只有一条路：**不在 append 里读文件**，即把 seq 放进进程内游标
（方案 A 的变体）。但 `memory/ledger.py:237/260` 每次都 `JsonlWriter(...)` 新建实例，
实例级缓存天然失效，缓存必须做成模块级；而且游标与磁盘一旦不同步就会发重复 seq
（除非每次写前 `stat` 校验 size，又回到一次 syscall）。**收益 ~8 ms/次，代价是引入一个
新的正确性假设**，按"不引入新故障模式"的口径没有做。

如仍要压这 8 ms，更划算的方向是先排查是不是杀软实时扫描：把 `%DATA_DIR%` 加入
Windows Defender 排除项，再跑一次 §8.3 的对照 —— 若"读同一文件"从 8.3 ms 掉到 0.4 ms，
就是扫描开销，不需要改代码。

### 8.4 验收

`scripts/check_store.py` JSONL 段 **31 条全 PASS**，其中新增 7 条：

| 断言 | 覆盖 |
|---|---|
| `jsonl_tail_cross_day` | 今天文件不存在时，从昨天文件尾部续 seq（11） |
| `jsonl_tail_partial_line` | 最后一行是崩溃半行 → 取上一条 seq（42 → 43） |
| `jsonl_tail_all_bad_fallback` | 尾部整段坏数据 → 回退到更早文件，不从 1 重来 |
| `jsonl_tail_big_line` | 单行 12 KB > 8 KB 初始窗口 → 扩窗后取到 |
| `jsonl_tail_empty_dir` | 空目录返回 1 |
| `jsonl_tail_perf_seq` | 28 MB 合成数据 seq 正确（120×20+1） |
| `jsonl_tail_perf_under_50ms` | 同上，耗时 < 50 ms（旧实现约 400 ms） |

另：`main_router.py:607` 的 `last_seq_before = turns.next_seq() - 1` 语义不变（返回值与新实现一致）；
`memory/ledger.py` 的 `append_consolidated` / `ReadRecords` 回读在隔离脚本中验证通过
（turns seq 1,2 → consolidated seq 3 → 关键词命中 1 条）。

### 8.5 仍未处理

`scripts/check_contracts.py` 整体仍卡在 `task_json_carries_chat_id`：两个线程同时
`create_session` 时 SQLite 首连发生 WAL 竞争，其中一个抛异常导致 `dispatched` 缺键
（`KeyError: 'A'`）。这是审计里"4 线程写 25 条只落 25 条"的同一根因，属存储层问题，
本次未动；也因此该脚本里 memory / jsonl 段改用隔离脚本单独验证。

顺手修了该脚本一处过期调用：`build_memory_router_tools(settings=..., session_service=None)`
—— 函数签名早已没有 `session_service` 参数，前面的并发缺陷一旦修好这里就会 `TypeError`。

