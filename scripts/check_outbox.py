"""M-GATEWAY 验收脚本：outbox 发送线程的抗异常能力（P0-6 回归）。

用法：
    python scripts/check_outbox.py

不连真实 Redis：用桩对象替换 `xreadgroup` / `xack` / `xadd`，只验证语义：

- 单条消息处理抛异常时，常驻线程**不得退出**（这正是历史缺陷：线程一死，
  进程、飞书长连接、心跳全都正常，但再也没有任何外发）；
- 重试耗尽后进死信：计数、回传 `system/send_failed`、ACK 出队；
- Redis 返回结构异常（缺索引 / 条目不是二元组 / fields 不是 dict）只跳过该条；
- `ensure_sender_alive()` 能重建已退出的线程，且 `_stopping=True` 时不重建；
- 正文已发出但幂等键回填失败时不重发（否则用户收到重复消息）。

退出码 0 表示全部通过。
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main_gateway as mg  # noqa: E402
from bus.envelope import event_to_redis, new_event  # noqa: E402

FAILURES: list[str] = []

# 退避置零，避免用例等待（真实值是 0.5s / 2.0s）
mg.OUTBOX_HANDLE_BACKOFF_S = (0.0, 0.0)


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def section(title: str) -> None:
    print(f"\n--- {title} ---")


class FakeRedis:
    def __init__(self, get_value=None, set_raises=False):
        self.added: list[dict] = []
        self.deleted: list[str] = []
        self._get_value = get_value
        self._set_raises = set_raises

    def get(self, key):
        return self._get_value

    def set(self, *args, **kwargs):
        if self._set_raises:
            raise ConnectionError("redis blip on set")
        return True

    def delete(self, key):
        self.deleted.append(key)

    def xadd(self, stream, fields, **kwargs):
        self.added.append(fields)
        return "1-1"


class FakeReader:
    """按 Redis 真实返回结构喂数据：[[stream, [[entry_id, fields], ...]]]。"""

    def __init__(self, script=None):
        self.script = list(script or [])
        self.acked: list[str] = []

    def xreadgroup(self, *args, **kwargs):
        if self.script:
            return self.script.pop(0)
        time.sleep(0.01)   # 避免空转把 CPU 打满
        return None

    def xack(self, stream, group, entry_id):
        self.acked.append(entry_id)

    def xgroup_create(self, *args, **kwargs):
        return None


class BoomChannel:
    """渠道 SDK 直接抛错。"""

    def send_text(self, **kwargs):
        raise RuntimeError("feishu sdk exploded")

    def send_image(self, **kwargs):
        raise RuntimeError("img boom")

    def send_file(self, **kwargs):
        raise RuntimeError("file boom")


class OkChannel:
    class _Result:
        ok = True
        error = ""
        code = 200
        message_id = "om_ok"

    def __init__(self):
        self.calls = 0

    def send_text(self, **kwargs):
        self.calls += 1
        return self._Result()

    def send_image(self, **kwargs):
        return self._Result()

    def send_file(self, **kwargs):
        return self._Result()


def make_sender(channel, reader, redis_stub=None) -> mg.OutboxSender:
    sender = mg.OutboxSender(channel=channel, command_client=FakeRedis(), read_client=reader)
    if redis_stub is not None:
        sender._redis = redis_stub
    return sender


GOOD_EVENT = new_event(
    "router_message",
    "router",
    {"chat_id": "c1", "text": "hello", "idem_slot": "s1"},
)
GOOD_FIELDS = event_to_redis(GOOD_EVENT)


def main() -> int:
    section("坏 payload：线程不退出、进死信、ACK")
    reader = FakeReader([[["s", [["1-0", {"data": b"not-json-at-all"}]]]]])
    sender = make_sender(BoomChannel(), reader, FakeRedis())
    sender.start()
    time.sleep(0.4)
    check("坏 payload 后线程仍存活", sender.is_alive())
    check("坏 payload 计入死信", sender.dead_letters == 1, f"dead_letters={sender.dead_letters}")
    check("坏 payload 已 ACK 出队", reader.acked == ["1-0"], f"acked={reader.acked}")
    sender.stop()
    check("stop() 后线程退出", not sender.is_alive())

    section("渠道抛异常：线程不退出、回传 send_failed")
    reader = FakeReader([[["s", [["2-0", GOOD_FIELDS]]]]])
    redis_stub = FakeRedis()
    sender = make_sender(BoomChannel(), reader, redis_stub)
    sender.start()
    time.sleep(0.4)
    check("渠道异常后线程仍存活", sender.is_alive())
    check("渠道异常计入死信", sender.dead_letters == 1, f"dead_letters={sender.dead_letters}")
    check("渠道异常已 ACK 出队", reader.acked == ["2-0"], f"acked={reader.acked}")
    check("向 router 回传了一条事件", len(redis_stub.added) == 1, f"n={len(redis_stub.added)}")
    if redis_stub.added:
        payload = str(redis_stub.added[0])
        check("回传事件是 send_failed", "send_failed" in payload and "dead_letter" in payload)
    sender.stop()

    section("正常消息：发送成功、ACK、无死信")
    reader = FakeReader([[["s", [["3-0", GOOD_FIELDS]]]]])
    sender = make_sender(OkChannel(), reader)
    sender.start()
    time.sleep(0.4)
    check("正常消息无死信", sender.dead_letters == 0, f"dead_letters={sender.dead_letters}")
    check("正常消息已 ACK", reader.acked == ["3-0"], f"acked={reader.acked}")
    check("正常消息后线程存活", sender.is_alive())
    sender.stop()

    section("Redis 返回结构异常：只跳过该条")
    entries = [{"a": 1}, ("4-0",), ("4-1", GOOD_FIELDS)]
    reader = FakeReader([[["s", entries]]])
    sender = make_sender(OkChannel(), reader)
    sender.start()
    time.sleep(0.4)
    check("坏条目不影响线程", sender.is_alive())
    check("只 ACK 合法条目", reader.acked == ["4-1"], f"acked={reader.acked}")
    sender.stop()

    reader = FakeReader([[("unexpected",)]])   # 缺 [1] 索引
    sender = make_sender(OkChannel(), reader)
    sender.start()
    time.sleep(0.3)
    check("xreadgroup 结构缺索引不崩线程", sender.is_alive())
    sender.stop()

    section("看护：重建已退出的发送线程")
    gateway = mg.Gateway.__new__(mg.Gateway)
    gateway._stopping = False
    gateway._channel = OkChannel()
    gateway._redis = FakeRedis()
    gateway._outbox_reader = FakeReader()
    gateway._sender = make_sender(OkChannel(), FakeReader())
    check("重建前线程未启动", not gateway._sender.is_alive())
    gateway.ensure_sender_alive()
    time.sleep(0.2)
    check("重建后线程存活", gateway._sender.is_alive())
    gateway._stopping = True
    gateway._sender.stop()

    gateway._stopping = True
    gateway._channel = OkChannel()
    gateway._redis = FakeRedis()
    gateway._outbox_reader = FakeReader()
    before = make_sender(OkChannel(), FakeReader())
    gateway._sender = before
    gateway.ensure_sender_alive()
    check("_stopping=True 时不重建", gateway._sender is before)

    section("幂等键回填失败：正文不重发")
    channel = OkChannel()
    reader = FakeReader([[["s", [["5-0", GOOD_FIELDS]]]]])
    sender = make_sender(channel, reader, FakeRedis(get_value="pending", set_raises=True))
    sender.start()
    time.sleep(0.4)
    check("正文只发一次", channel.calls == 1, f"calls={channel.calls}")
    check("回填失败不算死信", sender.dead_letters == 0, f"dead_letters={sender.dead_letters}")
    check("回填失败仍 ACK", reader.acked == ["5-0"], f"acked={reader.acked}")
    sender.stop()

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} 项 -> {', '.join(FAILURES)}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
