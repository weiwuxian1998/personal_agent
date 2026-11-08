"""JSONL 追加写：按 Asia/Shanghai 日期轮转，seq 全局递增，逐条 fsync。

契约 §5.3：`open(..., "a", encoding="utf-8")` + 单行 JSON + `flush()` + `os.fsync()`；
**永不回写旧记录**（崩溃时最坏丢最后一行，不会破坏历史）。

只有 router 进程写这些文件；`read_records` 供 router 自己与 `read_session` 工具只读使用。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Iterator

from contracts import TZ_SHANGHAI, now_iso


class JsonlWriter:
    """单个前缀的追加写入器（例如 `inbox` / `turns` / `tools`）。"""

    def __init__(self, directory: Path, prefix: str) -> None:
        self.directory = Path(directory)
        self.prefix = prefix
        self.directory.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- 文件定位
    def _files(self) -> list[Path]:
        return sorted(self.directory.glob(f"{self.prefix}-*.jsonl"))

    def current_path(self) -> Path:
        """当前上海日期对应的文件（日期轮转的唯一依据）。"""
        day = datetime.now(TZ_SHANGHAI).strftime("%Y-%m-%d")
        return self.directory / f"{self.prefix}-{day}.jsonl"

    def next_seq(self) -> int:
        """扫描现有文件的最大 seq + 1；文件不存在/为空时从 1 开始。"""
        max_seq = 0
        for path in self._files():
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        # 崩溃可能留下半行；seq 计算跳过坏行，不阻塞后续写入
                        continue
                    seq = record.get("seq")
                    if isinstance(seq, int) and seq > max_seq:
                        max_seq = seq
        return max_seq + 1

    # ---------------------------------------------------------------- 写入
    def append(self, record: dict) -> int:
        """追加一条记录，自动补 `v` / `seq` / `ts`，返回 seq。

        注意：只追加，不修改已有文件内容；`flush + fsync` 保证进程崩溃后
        已返回的记录仍在磁盘上。
        """
        data = dict(record)
        data.setdefault("v", 1)
        seq = data.get("seq")
        if not isinstance(seq, int) or seq <= 0:
            seq = self.next_seq()
        data["seq"] = seq
        data.setdefault("ts", now_iso())

        path = self.current_path()
        line = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return seq


def append_turn(turn: dict, *, directory: Path) -> int:
    """便捷函数：把一轮对话写入 `turns-<date>.jsonl`。"""
    return JsonlWriter(Path(directory), "turns").append(turn)


def read_records(
    directory: Path,
    *,
    since_seq: int = 0,
    limit: int | None = None,
) -> Iterator[dict]:
    """按文件日期、文件内顺序读取记录，只返回 `seq > since_seq` 的行。

    只读，不会创建文件；目录不存在时返回空迭代。
    """
    directory = Path(directory)
    if not directory.exists():
        return
    files = sorted(directory.glob("*.jsonl"))
    emitted = 0
    for path in files:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                seq = record.get("seq")
                if isinstance(seq, int) and seq <= since_seq:
                    continue
                yield record
                emitted += 1
                if limit is not None and emitted >= limit:
                    return
