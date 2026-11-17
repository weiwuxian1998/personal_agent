"""按大小轮转 assistant 自己的日志（gateway / router / worker 的 stdout 与 live 日志）。

日志目录约定（与 `scripts/gateway.ps1` / 各进程的写法保持一致）：
- 仓库 `logs/`：`gateway-live.log`（cmd `>>` 重定向的 stdout/stderr）与 `gateway.pid`；
- 运行目录 `assistant-data/logs/`：`router-<date>.log`、`worker-<task>.log`。

轮转策略：
- 触发：单文件 ≥ `LOG_ROTATE_MAX_BYTES`（默认 20 MiB）；
- 份数：`.1` … `.N`（默认 `LOG_ROTATE_BACKUPS=5`），更旧的删除；
- 不压缩：单份已经切到 20 MiB 量级，压缩收益有限且会带来额外 CPU/磁盘抖动。

三种轮转方式（取决于文件是否仍被持有）：
1. `rotate_closed_file`：gateway 未启动时由 `gateway.ps1 start` 调用，直接改名（便宜）；
2. `rotate_active_file`：router/worker 的日志由 Python `open(..., "ab")` 持有，
   copytruncate（复制成 `.1` 再截断）。**绝不改名**——改名后写入方会继续写进 `.1`；
3. `rotate_stdout`：gateway 自己的 live 日志被 cmd 的 `>>` 句柄持有（只允许读共享），
   外部无法 copytruncate；由 gateway 进程用继承来的 fd 1 直接截断并 lseek 回 0。

参数全部来自 `config.settings`（`LOG_ROTATE_*`）。CLI 只做一次扫描（默认按「文件未被持有」处理）：
    python log_rotation.py            # 扫一遍并轮转
    python log_rotation.py --dry-run  # 只打印将要做什么
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
from pathlib import Path

from config import settings

REPO_ROOT = Path(__file__).resolve().parent

# 明确只处理「assistant 自己产生、可能被刷屏」的日志；
# security.log 属于安全留痕，不参与轮转，避免审计记录被切走。
ROUTER_WORKER_PATTERNS = ("router-*.log", "worker-*.log")


def _shift_backups(path: Path, keep: int) -> None:
    """把 `.1..keep` 后移一位，并丢掉超出的最旧份。"""
    oldest = path.with_name(f"{path.name}.{keep}")
    if oldest.exists():
        oldest.unlink(missing_ok=True)
    for index in range(keep - 1, 0, -1):
        source = path.with_name(f"{path.name}.{index}")
        if source.exists():
            source.replace(path.with_name(f"{path.name}.{index + 1}"))


def _backup_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.1")


def _too_small(path: Path, max_bytes: int, keep: int) -> bool:
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return True
    return keep <= 0 or size < max_bytes


def rotate_closed_file(path: Path, *, max_bytes: int, keep: int) -> str:
    """文件当前没有写入方：直接改名；若其实被持有则退化为 copytruncate。"""
    if not path.exists():
        return "missing"
    if _too_small(path, max_bytes, keep):
        return "skipped"
    _shift_backups(path, keep)
    try:
        path.replace(_backup_path(path))
        return "renamed"
    except PermissionError:
        return rotate_active_file(path, max_bytes=max_bytes, keep=keep)


def rotate_active_file(path: Path, *, max_bytes: int, keep: int) -> str:
    """文件可能仍被写入：copytruncate（不改名，避免活跃句柄继续写进 `.1`）。"""
    if not path.exists():
        return "missing"
    if _too_small(path, max_bytes, keep):
        return "skipped"
    _shift_backups(path, keep)
    backup = _backup_path(path)
    try:
        shutil.copyfile(path, backup)
        with open(path, "r+b") as handle:
            handle.truncate(0)
        return "copytruncate"
    except OSError as exc:
        return f"failed: {type(exc).__name__}: {exc}"


def stdout_is(path: Path) -> bool:
    """`path` 是否就是本进程 stdout 重定向到的文件。"""
    try:
        fd_stat = os.fstat(sys.stdout.fileno())
        file_stat = path.stat()
    except (OSError, ValueError):
        return False
    if not stat.S_ISREG(fd_stat.st_mode):
        return False
    return (fd_stat.st_dev, fd_stat.st_ino) == (file_stat.st_dev, file_stat.st_ino)


def rotate_stdout(path: Path, *, max_bytes: int, keep: int) -> str:
    """gateway 自己的 live 日志：cmd 的 `>>` 句柄只允许读共享，只能用继承的 fd 截断。"""
    if not path.exists():
        return "missing"
    if _too_small(path, max_bytes, keep):
        return "skipped"
    _shift_backups(path, keep)
    fd = sys.stdout.fileno()
    try:
        shutil.copyfile(path, _backup_path(path))  # 只读打开：cmd 的共享模式允许
        sys.stdout.flush()
        os.ftruncate(fd, 0)
        # cmd 的重定向句柄不是 O_APPEND：截断后必须把文件指针拨回 0，
        # 否则后续日志会写回旧偏移，文件变成稀疏大文件（已实测）。
        os.lseek(fd, 0, os.SEEK_SET)
        return "ftruncate"
    except OSError as exc:
        return f"failed: {type(exc).__name__}: {exc}"


def _rotate_target(path: Path, *, max_bytes: int, keep: int, in_process: bool) -> str:
    if in_process:
        if stdout_is(path):
            return rotate_stdout(path, max_bytes=max_bytes, keep=keep)
        return rotate_active_file(path, max_bytes=max_bytes, keep=keep)
    return rotate_closed_file(path, max_bytes=max_bytes, keep=keep)


def rotate_all(*, in_process: bool = False, dry_run: bool = False) -> list[tuple[str, str, int]]:
    """扫描所有目标日志；返回 `(路径, 动作, 轮转前字节数)`，供调用方记一行日志。"""
    max_bytes = int(settings.log_rotate_max_bytes)
    keep = int(settings.log_rotate_backups)
    results: list[tuple[str, str, int]] = []
    targets: list[tuple[Path, bool]] = []
    live = REPO_ROOT / "logs" / "gateway-live.log"
    targets.append((live, in_process))
    logs_dir = settings.logs_dir
    if logs_dir.exists():
        for pattern in ROUTER_WORKER_PATTERNS:
            targets.extend((path, True) for path in sorted(logs_dir.glob(pattern)))
    for path, active in targets:
        if not path.is_file():
            continue
        try:
            before = path.stat().st_size
        except FileNotFoundError:
            continue
        if dry_run:
            action = "would-rotate" if before >= max_bytes else "skipped"
        else:
            action = _rotate_target(path, max_bytes=max_bytes, keep=keep, in_process=active)
        if action in ("renamed", "copytruncate", "ftruncate", "would-rotate") or action.startswith("failed"):
            results.append((str(path), action, before))
    return results


def main(argv: list[str]) -> int:
    dry_run = "--dry-run" in argv
    for path, action, before in rotate_all(dry_run=dry_run):
        print(f"[rotate] {path} {before} bytes -> {action}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
