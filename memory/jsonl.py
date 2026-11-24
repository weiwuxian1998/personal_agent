"""JSONL 追加写：按 Asia/Shanghai 日期轮转，seq 全局递增，逐条 fsync。

契约 §5.3：`open(..., "a", encoding="utf-8")` + 单行 JSON + `flush()` + `os.fsync()`；
**永不回写旧记录**（崩溃时最坏丢最后一行，不会破坏历史）。

只有 router 进程写这些文件；`read_records` 供 router 自己与 `read_session` 工具只读使用。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Iterator

from contracts import TZ_SHANGHAI, now_iso

logger = logging.getLogger(__name__)


# 反向扫尾的窗口大小：先读最后 8 KB（通常一两行就够），不够再翻倍扩窗。
# 上限 1 MB —— 超过仍找不到有效行说明文件尾部有成片坏数据，放弃该文件。
_TAIL_SCAN_MIN = 8 * 1024
_TAIL_SCAN_MAX = 1024 * 1024
_FILE_SUFFIX = ".jsonl"


class JsonlWriter:
    """单个前缀的追加写入器（例如 `inbox` / `turns` / `tools`）。

    seq 的前提：**全局单调递增、只追加不回写**（本模块自身保证）。
    因此最新文件最后一行的 seq 就是全局最大值，`next_seq()` 只需反向读尾部，
    不必像旧实现那样遍历全部历史文件逐行 `json.loads`（成本 O(历史总字节)，
    一年量级可达 1.5 s/次）。
    """

    def __init__(self, directory: Path, prefix: str) -> None:
        self.directory = Path(directory)
        self.prefix = prefix
        self.directory.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- 文件定位
    def _names_desc(self) -> list[str]:
        """本前缀下的文件名，按日期倒序（新 → 旧）。

        用 `os.scandir` + 字符串排序，而不是 `glob` + `sorted(Path)`：后者要为每个
        目录项构造 Path 并做 fnmatch，文件攒到一年（365 个）时光这一项就 3 ms。
        文件名形如 `<prefix>-YYYY-MM-DD.jsonl`，字典序与日期序一致。
        """
        prefix = f"{self.prefix}-"
        names: list[str] = []
        try:
            with os.scandir(self.directory) as entries:
                for entry in entries:
                    name = entry.name
                    if name.startswith(prefix) and name.endswith(_FILE_SUFFIX):
                        names.append(name)
        except OSError:
            return []
        names.sort(reverse=True)
        return names

    def current_path(self) -> Path:
        """当前上海日期对应的文件（日期轮转的唯一依据）。"""
        day = datetime.now(TZ_SHANGHAI).strftime("%Y-%m-%d")
        return self.directory / f"{self.prefix}-{day}.jsonl"

    def _tail_seq(self, path: Path) -> int | None:
        """反向分块读某个文件的尾部，返回最后一个有效记录的 seq。

        不整体读入内存：从文件末尾起读 `_TAIL_SCAN_MIN` 字节，反向逐行解析；
        当前窗口内没有有效行（崩溃留下半行、单行超长、全是空行）就把窗口翻 4 倍
        继续向前翻，直到覆盖到文件头或达到 `_TAIL_SCAN_MAX`。
        窗口起点不为 0 时，切出的第一行必然是被截断的残片，直接丢弃。
        文件不存在 / 为空 / 尾部无可解析记录时返回 None。
        """
        try:
            fh = open(path, "rb")
        except OSError:
            return None
        with fh:
            try:
                size = os.fstat(fh.fileno()).st_size
            except OSError:
                return None
            if size <= 0:
                return None

            window = min(_TAIL_SCAN_MIN, size)
            while True:
                start = max(0, size - window)
                fh.seek(start)
                chunk = fh.read()
                lines = chunk.decode("utf-8", errors="replace").splitlines()
                if start > 0 and lines:
                    lines = lines[1:]  # 首行大概率是半个 UTF-8 序列 / 半条记录
                for line in reversed(lines):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        # 崩溃可能留下半行；跳过坏行继续前翻，不阻塞后续写入
                        continue
                    seq = record.get("seq")
                    if isinstance(seq, int) and seq > 0:
                        return seq
                if start == 0 or window >= _TAIL_SCAN_MAX:
                    return None
                window = min(window * 4, _TAIL_SCAN_MAX, size)

    def next_seq(self) -> int:
        """今日文件与日期最新文件尾部 seq 的较大者 + 1；没有可读历史时从 1 开始。

        正常情况下今日文件就是日期最新文件，只探一个文件尾部（恒定亚毫秒级）；
        时钟回拨等情况可能留下「日期晚于今天」的文件，此时 Append 写今日文件、
        而全局最大 seq 在那份未来文件里，故两者都探一次取 max，
        避免 seq 回退导致 `read_records(since_seq=)` 漏读。
        """
        current = self.current_path()
        names = self._names_desc()
        probe = [current]
        if names:
            latest = self.directory / names[0]
            if latest != current:
                probe.append(latest)

        best = 0
        for path in probe:
            seq = self._tail_seq(path)
            if seq is not None and seq > best:
                best = seq
        if best:
            return best + 1

        # 今日与最新文件都读不出 seq（为空或尾部损坏）→ 按日期倒序回退
        seen = {p.name for p in probe}
        for name in names:
            if name in seen:
                continue
            seq = self._tail_seq(self.directory / name)
            if seq is not None:
                return seq + 1
        return 1

    # ---------------------------------------------------------------- 写入
    def append(self, record: dict) -> int:
        """追加一条记录，自动补 `v` / `seq` / `ts`，返回 seq。

        注意：只追加，不修改已有文件内容；`flush + fsync` 保证进程崩溃后
        已返回的记录仍在磁盘上。

        写之前先看文件尾最后一个字节：**上一个进程可能在写半行时被杀**，此时
        文件尾没有 `\\n`。直接 `write(line + "\\n")` 会把新记录拼到那半行后面，
        变成 `{...,"seq":43,"pa{"v":1,...}` 这样一条永久不可解析的长行，
        崩一次就毁掉两条记录。所以尾字节不是换行时先补一个 `\\n`，
        让那半行独立成行（它本身已经坏掉，至少不连累新记录）。
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
        with open(path, "a+b") as fh:  # 追加 + 可读，用来探测尾字节
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            needs_break = False
            if size > 0:
                fh.seek(-1, os.SEEK_END)
                needs_break = fh.read(1) != b"\n"
            payload = ("\n" if needs_break else "") + line + "\n"
            fh.write(payload.encode("utf-8"))
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

    **坏行不中断读取**：崩溃可能留下半行（无换行、JSON 截断）或非对象的行，
    以前这里裸调 `json.loads`，一行坏数据就让整个生成器抛异常，`/api/turns`、
    `/api/inbox` 从此 500。现在逐行 try：解析失败的跳过并计数，读完一个文件
    打一条 WARNING；打开失败的文件整个跳过。宁可少读一条，不能整条链路挂掉。
    """
    directory = Path(directory)
    if not directory.exists():
        return
    files = sorted(directory.glob("*.jsonl"))
    emitted = 0
    for path in files:
        bad = 0
        try:
            # errors="replace"：文件尾若有被截断的多字节字符，strict 会抛
            # UnicodeDecodeError（同样是"一行坏数据杀死整个读取"）。
            fh = open(path, "r", encoding="utf-8", errors="replace")
        except OSError:
            logger.warning("jsonl 无法打开，已跳过：%s", path)
            continue
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (ValueError, RecursionError):
                    bad += 1  # JSONDecodeError ⊂ ValueError
                    continue
                if not isinstance(record, dict):
                    bad += 1
                    continue
                seq = record.get("seq")
                if isinstance(seq, int) and seq <= since_seq:
                    continue
                yield record
                emitted += 1
                if limit is not None and emitted >= limit:
                    return
        if bad:
            logger.warning("%s 有 %d 行无法解析（崩溃残留），已跳过", path.name, bad)
