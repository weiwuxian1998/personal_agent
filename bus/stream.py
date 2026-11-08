"""Redis Stream 封装：消费者组、批量读取、待处理消息认领（崩溃重放）。

本文件实现契约 §4.3 的 `Bus`；信封的序列化格式完全来自冻结的
`bus/envelope.py`（单字段 `data` = `Event.to_json()`），本文件不另建结构。

崩溃安全的关键：`read_batch` 只是把条目放进消费者组的 PEL；
只有 `ack()` 成功后才算消费完成。进程崩溃后，未 ACK 的条目仍留在 PEL，
由下次启动的 `claim_pending()`（XAUTOCLAIM）重新认领，不会丢任务。

注意：阻塞读与命令连接应分开（契约 §4.3）。`read_batch` / `drain_more`
的 `block_ms` 必须小于连接的 socket_timeout，调用方需自行保证
（仓库默认把读连接设为 `socket_timeout=None`）。
"""

from __future__ import annotations

from typing import Any

import redis

from bus.envelope import (
    DEDUPE_FEISHU_PREFIX,
    ROUTER_CONSUMER,
    ROUTER_GROUP,
    ROUTER_STREAM,
    ROUTER_STREAM_MAXLEN,
    event_from_redis,
    event_to_redis,
    worker_consumer,
    worker_group,
    worker_stream,
)
from contracts import Event

# 飞书消息去重默认保留 6 小时（与 config.settings.dedupe_ttl_s 默认值一致）
DEFAULT_DEDUPE_TTL_S = 6 * 3600


def _text(value: Any) -> str:
    """redis-py 在 decode_responses=False 时返回 bytes；统一转成 str。"""
    return value.decode() if isinstance(value, bytes) else str(value)


def _parse_entries(raw: Any) -> list[tuple[str, Event]]:
    """把 XREADGROUP / XAUTOCLAIM 的条目列表转成 `(entry_id, Event)`。"""
    out: list[tuple[str, Event]] = []
    for entry_id, fields in raw or []:
        normalized = {_text(k): v for k, v in fields.items()}
        out.append((_text(entry_id), event_from_redis(normalized)))
    return out


class Bus:
    """router 侧 Stream 操作。worker 的就绪/结果回传同样复用 router stream。"""

    def __init__(self, client: redis.Redis) -> None:
        self.client = client
        self.stream = ROUTER_STREAM
        self.group = ROUTER_GROUP
        self.consumer = ROUTER_CONSUMER

    # ---------------------------------------------------------------- 消费者组
    def ensure_router_group(self) -> None:
        """幂等创建 router 消费者组；stream 不存在时一并创建。"""
        try:
            self.client.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except redis.ResponseError as exc:
            # BUSYGROUP = 组已存在，属正常情况；其他错误（如 NOGROUP 之外的）继续抛
            if "BUSYGROUP" not in str(exc):
                raise

    def ensure_worker_group(self, task_id: str) -> None:
        """为某个 worker 任务创建独立消费者组（router → worker 指令通道）。"""
        try:
            self.client.xgroup_create(worker_stream(task_id), worker_group(task_id), id="0", mkstream=True)
        except redis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def worker_consumer_name(self, task_id: str) -> str:
        """worker 侧读取自己指令流时使用的 consumer 名（与组规则保持一致）。"""
        return worker_consumer(task_id)

    # ---------------------------------------------------------------- 写入
    def xadd(
        self,
        event: Event,
        *,
        stream: str = ROUTER_STREAM,
        maxlen: int = ROUTER_STREAM_MAXLEN,
    ) -> str:
        """追加事件；`MAXLEN ~` 近似裁剪，避免长跑无限增长。"""
        entry_id = self.client.xadd(stream, event_to_redis(event), maxlen=maxlen, approximate=True)
        return _text(entry_id)

    # ---------------------------------------------------------------- 读取
    def read_batch(self, *, block_ms: int = 5000, count: int = 10) -> list[tuple[str, Event]]:
        """阻塞读一批未投递（`>`）消息；条目进入 PEL，等待 `ack()`。

        超时返回空列表（表示本轮没有新事件），不抛异常。
        """
        result = self.client.xreadgroup(
            self.group,
            self.consumer,
            {self.stream: ">"},
            count=count,
            block=block_ms,
        )
        if not result:
            return []
        return _parse_entries(result[0][1])

    def drain_more(
        self,
        first: list[tuple[str, Event]],
        *,
        debounce_ms: int = 200,
        max_batch: int = 10,
    ) -> list[tuple[str, Event]]:
        """在 `first` 之后继续非阻塞补读，直到静默 `debounce_ms` 或达到 `max_batch`。

        目的：同一批到达的多个事件（例如用户连发两条 + worker 结果）合并成一轮，
        避免为每条消息各跑一次 agent。`block=debounce_ms` 天然实现了防抖。
        """
        batch = list(first)
        while len(batch) < max_batch:
            remaining = max_batch - len(batch)
            result = self.client.xreadgroup(
                self.group,
                self.consumer,
                {self.stream: ">"},
                count=remaining,
                block=debounce_ms,
            )
            if not result:
                break
            batch.extend(_parse_entries(result[0][1]))
        return batch

    def ack(self, entries: list[tuple[str, Event]]) -> None:
        """确认消费完成；只有这里成功，消息才会离开 PEL。"""
        if not entries:
            return
        pipe = self.client.pipeline()
        for entry_id, _event in entries:
            pipe.xack(self.stream, self.group, entry_id)
        pipe.execute()

    def claim_pending(
        self,
        *,
        min_idle_ms: int = 60000,
        count: int = 10,
    ) -> list[tuple[str, Event]]:
        """认领 PEL 中空闲超过 `min_idle_ms` 的条目（崩溃后重放在这里发生）。

        XAUTOCLAIM 会把条目所属 consumer 改成本实例的 consumer，并重置 idle。
        调用方拿到的条目必须重新处理，处理成功后仍然要 `ack()`。
        """
        result = self.client.xautoclaim(
            self.stream,
            self.group,
            self.consumer,
            min_idle_time=min_idle_ms,
            start_id="0-0",
            count=count,
        )
        if not result or len(result) < 2:
            return []
        return _parse_entries(result[1])

    def pel_nonempty(self) -> bool:
        """PEL 是否还有未确认条目（router 退出前必须为空）。"""
        info = self.client.xpending(self.stream, self.group)
        if isinstance(info, dict):
            return int(info.get("pending", 0)) > 0
        # 旧版本 redis-py 返回 summary 列表形式
        return bool(info)

    def xlen(self) -> int:
        return int(self.client.xlen(self.stream))


def try_dedupe(client: redis.Redis, dedupe_key: str, *, ttl_s: int = DEFAULT_DEDUPE_TTL_S) -> bool:
    """原子去重占位：返回 True 表示第一次见到该键（可以入队）。

    `SET NX EX` 单条命令完成"查 + 写"，多进程并发也不会重复入队。
    飞书去重键用 `message_id`（不是 event_id），前缀见 `bus/envelope.py`。
    入队失败时调用方必须删除该键，否则消息会被永久丢弃。
    """
    if not dedupe_key:
        return True
    key = dedupe_key if dedupe_key.startswith(DEDUPE_FEISHU_PREFIX) else f"{DEDUPE_FEISHU_PREFIX}{dedupe_key}"
    return bool(client.set(key, "1", nx=True, ex=ttl_s))
