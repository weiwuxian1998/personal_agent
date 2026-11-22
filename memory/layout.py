"""记忆设计的目录布局（docs/MEMORY_DESIGN.md §6 目录树）。

config.py 是冻结文件（契约 §11 变更流程），新增目录一律在这里派生：

- 素材/记账区（memory 区之外，"素材不是记忆"）：
  `router/notes/`、`router/transcript/`、`router/ledger/`；
- 记忆区（memory_dir 为既有配置项）：
  `memory/index.md`、`memory/threads/`、`memory/journal/`、`memory/scratch/`。

只有 router / 记忆整理 worker / 控制台读这些路径；本模块只算路径，不建目录
（建目录是写入方的事：JsonlWriter 自带 mkdir，fs 工具 Write 自带 makedirs）。
"""

from __future__ import annotations

from pathlib import Path

from config import Settings


# ---------------------------------------------------------------- 素材 / 记账区
def notes_dir(settings: Settings) -> Path:
    """router 每日笔记（§5）：一天一份 `YYYY-MM-DD.md`，router 用 fs Write/Edit 写。"""
    return Path(settings.router_dir) / "notes"


def transcript_dir(settings: Settings) -> Path:
    """系统自动记录的全量消息按天流水（§4）：`transcript-YYYY-MM-DD.jsonl`。"""
    return Path(settings.router_dir) / "transcript"


def ledger_dir(settings: Settings) -> Path:
    """turns/inbox 记账流水（§4，控制台 `/api/turns` `/api/inbox` 读取）。"""
    return Path(settings.router_dir) / "ledger"


# ---------------------------------------------------------------- 记忆区
def index_path(settings: Settings) -> Path:
    """注入层唯一常驻页 `index.md`（§6.1：≤200 行 / ≤8KB）。"""
    return Path(settings.memory_dir) / "index.md"


def threads_dir(settings: Settings) -> Path:
    """进行中事项按天快照（§6.1）：`threads-YYYY-MM-DD.md`，最新一份 = 当前状态。"""
    return Path(settings.memory_dir) / "threads"


def journal_dir(settings: Settings) -> Path:
    """每夜整理日志（§6 目录树）：当晚做了什么、covers 区间、版本变化。"""
    return Path(settings.memory_dir) / "journal"


def scratch_dir(settings: Settings) -> Path:
    """整理 worker 的中间草稿区（§7）：`scratch/<day>/`，完成后清理。"""
    return Path(settings.memory_dir) / "scratch"
