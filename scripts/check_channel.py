"""M-CHANNEL 验收脚本（契约 §6 / §13 第 4 条）。

用法：`python scripts/check_channel.py`，退出码 0 才算交付。

覆盖范围：
- console 渠道收发闭环；
- 飞书事件解析（单聊 / 群聊 @ 清洗 / 机器人自身消息过滤 / 非文本 / 坏 JSON / 空文本）；
- 进程内去重（重复投递只回调一次；回调失败后释放去重键）；
- 文本分段（不丢字、不超长、按标点优先切）；
- 发送成功 / 飞书返回失败码 / 网络异常 / 多段中途失败；
- `SendResult` 结构符合冻结定义。

**不访问网络、不发送真实消息**：飞书发送全部用假 API 客户端注入；
只有显式设置 `FEISHU_LIVE=1` 且提供 `FEISHU_LIVE_CHAT_ID` 时才做一次真实发送探测。
"""

from __future__ import annotations

import contextlib
import importlib.metadata as metadata
import io
import json
import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from channel.base import Channel  # noqa: E402
from channel.console import ConsoleChannel  # noqa: E402
from contracts import IncomingMessage, SendResult, ulid  # noqa: E402

try:
    from channel.feishu import (  # noqa: E402
        DEFAULT_MAX_TEXT_CHARS,
        FeishuChannel,
        MessageDeduper,
        parse_p2_message,
        split_text,
    )
except Exception as exc:  # noqa: BLE001 缺少 lark-oapi 时给出可读结论而不是裸 traceback
    FEISHU_IMPORT_ERROR: Exception | None = exc
else:
    FEISHU_IMPORT_ERROR = None

RESULTS: list[tuple[str, bool, str]] = []


# --------------------------------------------------------------------------- 测试辅助
def make_event(
    *,
    message_id: str = "om_check_001",
    event_id: str = "evt_check_001",
    text: str = "你好，自检",
    sender_type: str | None = "user",
    message_type: str = "text",
    chat_type: str = "p2p",
    chat_id: str = "oc_check",
    mentions: list | None = None,
    content: str | None = None,
    message_create_time: str | None = "1760000000000",
    header_create_time: str | None = "1760000000100",
    thread_id: str | None = None,
) -> dict:
    """构造与飞书 v2 事件结构一致的 dict（不依赖 SDK 之外的真实凭证）。"""
    if content is None:
        if message_type == "text":
            content = json.dumps({"text": text}, ensure_ascii=False)
        else:
            content = json.dumps({"image_key": "img_check"}, ensure_ascii=False)

    message: dict = {
        "message_id": message_id,
        "chat_id": chat_id,
        "chat_type": chat_type,
        "message_type": message_type,
        "content": content,
    }
    if message_create_time is not None:
        message["create_time"] = message_create_time
    if thread_id is not None:
        message["thread_id"] = thread_id
    if mentions:
        message["mentions"] = mentions

    header: dict = {
        "event_id": event_id,
        "event_type": "im.message.receive_v1",
        "token": "fake-token",
        "app_id": "cli_check_fake",
        "tenant_key": "fake_tenant",
    }
    if header_create_time is not None:
        header["create_time"] = header_create_time

    return {
        "schema": "2.0",
        "header": header,
        "event": {
            "sender": {
                "sender_id": {"open_id": "ou_check_user", "user_id": "u_check_user"},
                "sender_type": sender_type,
                "tenant_key": "fake_tenant",
            },
            "message": message,
        },
    }


def payload(event: dict) -> bytes:
    return json.dumps(event, ensure_ascii=False).encode("utf-8")


class _FakeResp:
    """替身：飞书 OpenAPI 返回对象的最小接口（success/code/msg/data）。"""

    def __init__(
        self,
        *,
        ok: bool = True,
        code: int = 0,
        msg: str = "",
        message_id: str = "",
        data_present: bool = True,
    ) -> None:
        self._ok = ok
        self.code = code
        self.msg = msg
        self.data = (
            types.SimpleNamespace(message_id=message_id) if data_present and message_id else None
        )

    def success(self) -> bool:
        return self._ok

    def get_log_id(self) -> str:
        return "fake-log-id"


class _FakeMessageApi:
    """替身：记录 reply/create 调用，并按队列返回预设响应（可为异常）。"""

    def __init__(self, responses: list | None = None) -> None:
        self.calls: list[dict] = []
        self._responses = list(responses or [])

    def _next(self, kind: str, **payload) -> _FakeResp:
        self.calls.append({"kind": kind, **payload})
        if not self._responses:
            return _FakeResp(ok=True, message_id=f"om_out_{len(self.calls)}")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def reply(self, request) -> _FakeResp:
        return self._next(
            "reply",
            message_id=request.message_id,
            content=request.request_body.content,
        )

    def create(self, request) -> _FakeResp:
        return self._next(
            "create",
            receive_id=request.request_body.receive_id,
            receive_id_type=request.receive_id_type,
            content=request.request_body.content,
        )


def make_fake_channel(responses: list | None = None) -> tuple[FeishuChannel, _FakeMessageApi]:
    channel = FeishuChannel("cli_check_fake", "fake_secret_value")
    fake_message_api = _FakeMessageApi(responses)
    channel._api = types.SimpleNamespace(
        im=types.SimpleNamespace(v1=types.SimpleNamespace(message=fake_message_api))
    )
    return channel, fake_message_api


def check(name: str, func) -> None:
    try:
        detail = func() or ""
    except Exception as exc:  # noqa: BLE001 验收脚本必须兜住一切，不能停在第一个失败
        RESULTS.append((name, False, f"{type(exc).__name__}: {exc}"))
        print(f"[FAIL] {name} —— {type(exc).__name__}: {exc}")
        import traceback

        traceback.print_exc(limit=3)
        return
    RESULTS.append((name, True, detail))
    if detail.startswith("SKIP"):
        print(f"[SKIP] {name} —— {detail}")
    else:
        print(f"[PASS] {name}" + (f" —— {detail}" if detail else ""))


# --------------------------------------------------------------------------- 1. 依赖
def t_dependencies() -> str:
    assert sys.version_info >= (3, 10), "需要 Python >= 3.10"
    assert FEISHU_IMPORT_ERROR is None, f"导入 channel.feishu 失败: {FEISHU_IMPORT_ERROR}"
    version = metadata.version("lark-oapi")
    assert version == "1.7.5", f"lark-oapi 版本漂移: {version}（契约锁定 1.7.5）"
    return f"Python {sys.version.split()[0]} | lark-oapi={version}"


# ------------------------------------------------------------------- 2. console 闭环
def t_console_channel() -> str:
    channel = ConsoleChannel()
    assert isinstance(channel, Channel), "ConsoleChannel 未实现 Channel 抽象"

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        result = channel.send_text(chat_id="console", text="你好", reply_to_message_id="om_user")
    assert isinstance(result, SendResult), "send_text 必须返回 SendResult"
    assert result.ok and len(result.message_id) == 26, f"SendResult 异常: {result}"
    assert channel.sent[-1]["text"] == "你好"
    assert channel.sent[-1]["reply_to_message_id"] == "om_user"
    assert "你好" in buffer.getvalue(), "console 没有把消息打印到 stdout"

    received: list[IncomingMessage] = []
    original_stdin = sys.stdin
    try:
        sys.stdin = io.StringIO("第一行\n\n第二行\n/quit\n第三行\n")
        channel.run_forever(received.append)
    finally:
        sys.stdin = original_stdin

    assert len(received) == 2, f"stdin 收消息数量错误: {len(received)}"
    assert [m.text for m in received] == ["第一行", "第二行"]
    assert all(m.channel == "console" and m.chat_id == "console" for m in received)
    assert all(m.chat_type == "p2p" and m.create_time > 0 for m in received)
    assert received[0].message_id != received[1].message_id, "console message_id 必须唯一"
    return "stdout 发送 + stdin 接收（空行跳过、/quit 退出）"


# --------------------------------------------------------------- 3. 飞书单聊事件解析
def t_parse_p2p() -> str:
    channel = FeishuChannel("cli_check_fake", "fake_secret_value")
    received: list[IncomingMessage] = []
    dispatcher = channel.build_dispatcher(received.append)

    dispatcher._do_without_validation(
        payload(make_event(thread_id="omt_check_001", message_create_time="1760000123456"))
    )

    assert len(received) == 1, "单聊事件没有被回调"
    msg = received[0]
    assert msg.channel == "feishu"
    assert msg.chat_id == "oc_check" and msg.chat_type == "p2p"
    assert msg.message_id == "om_check_001" and msg.event_id == "evt_check_001"
    assert msg.sender_id == "ou_check_user"
    assert msg.text == "你好，自检"
    assert msg.create_time == 1760000123456, f"create_time 解析错误: {msg.create_time}"
    assert msg.thread_id == "omt_check_001", f"thread_id 解析错误: {msg.thread_id}"

    # 直接调用解析函数：缺失 event / None 都必须安全返回 None
    assert parse_p2_message(None) is None
    assert parse_p2_message(types.SimpleNamespace(event=None)) is None
    return "单聊字段（chat/message/event/sender/create_time/thread_id）全部正确"


# --------------------------------------------------------------- 4. 群聊 @ 清洗
def t_parse_group_mentions() -> str:
    channel = FeishuChannel("cli_check_fake", "fake_secret_value")
    received: list[IncomingMessage] = []
    dispatcher = channel.build_dispatcher(received.append)

    dispatcher._do_without_validation(
        payload(
            make_event(
                message_id="om_check_group_1",
                event_id="evt_check_group_1",
                chat_id="oc_group_check",
                chat_type="group",
                text="@_user_1 帮我看看时间",
                mentions=[
                    {
                        "key": "@_user_1",
                        "id": {"open_id": "ou_check_bot"},
                        "name": "助手",
                        "tenant_key": "fake_tenant",
                    }
                ],
            )
        )
    )
    assert len(received) == 1, "群聊事件没有被回调"
    assert received[0].chat_type == "group"
    assert received[0].text == "帮我看看时间", f"@ 占位符没清干净: {received[0].text!r}"

    # 只 @ 不说话 → 清洗后为空 → 不产生消息
    dispatcher._do_without_validation(
        payload(
            make_event(
                message_id="om_check_group_2",
                event_id="evt_check_group_2",
                chat_id="oc_group_check",
                chat_type="group",
                text="@_user_1",
                mentions=[{"key": "@_user_1", "name": "助手"}],
            )
        )
    )
    assert len(received) == 1, "仅 @ 机器人的空文本不应入队"
    return "群聊 @_user_1 清洗正确；清洗后空文本被丢弃"


# --------------------------------------------------------- 5. 机器人 / 非文本 / 坏内容
def t_parse_filters() -> str:
    channel = FeishuChannel("cli_check_fake", "fake_secret_value")
    received: list[IncomingMessage] = []
    dispatcher = channel.build_dispatcher(received.append)

    # 机器人 / 应用自己发的消息（sender_type=app）必须过滤，否则自问自答死循环
    dispatcher._do_without_validation(
        payload(make_event(message_id="om_bot_1", event_id="evt_bot_1", sender_type="app"))
    )
    # 非文本消息（图片）先跳过
    dispatcher._do_without_validation(
        payload(make_event(message_id="om_img_1", event_id="evt_img_1", message_type="image"))
    )
    # content 不是合法 JSON
    dispatcher._do_without_validation(
        payload(
            make_event(
                message_id="om_bad_json",
                event_id="evt_bad_json",
                content="{not-json",
            )
        )
    )
    # 纯空白文本
    dispatcher._do_without_validation(
        payload(
            make_event(
                message_id="om_blank",
                event_id="evt_blank",
                content=json.dumps({"text": "   \n  "}, ensure_ascii=False),
            )
        )
    )
    assert received == [], f"过滤规则失效，收到 {len(received)} 条消息"

    # sender_type 缺失（None）仍视为用户消息（契约 §6.2）
    dispatcher._do_without_validation(
        payload(make_event(message_id="om_no_type", event_id="evt_no_type", sender_type=None))
    )
    assert len(received) == 1, "sender_type=None 应按普通用户消息处理"
    return "机器人消息 / 图片 / 坏 JSON / 空白文本 全部过滤；sender_type=None 放行"


# ------------------------------------------------------------------- 6. 去重
def t_dedupe() -> str:
    channel = FeishuChannel("cli_check_fake", "fake_secret_value")
    received: list[IncomingMessage] = []
    dispatcher = channel.build_dispatcher(received.append)

    event = make_event(message_id="om_dup_1", event_id="evt_dup_1")
    dispatcher._do_without_validation(payload(event))
    dispatcher._do_without_validation(payload(event))  # 模拟飞书 ACK 失败后的重推
    assert len(received) == 1, f"重复投递没有被去重: 回调 {len(received)} 次"

    dispatcher._do_without_validation(
        payload(make_event(message_id="om_dup_2", event_id="evt_dup_2"))
    )
    assert len(received) == 2, "不同 message_id 不应被误判为重复"

    deduper = MessageDeduper(ttl_s=60)
    assert deduper.seen("k1") is False, "首次不应判定重复"
    assert deduper.seen("k1") is True, "第二次必须判定重复"
    deduper.forget("k1")
    assert deduper.seen("k1") is False, "forget 后应可再次处理"
    assert deduper.seen(None) is False, "空 key 不应参与去重"

    # 回调处理失败 → 释放本地去重键，下一次重推必须仍能进入回调
    calls: list[str] = []

    def flaky_handler(message: IncomingMessage) -> None:
        calls.append(message.message_id)
        if len(calls) == 1:
            raise RuntimeError("模拟 gateway 入队失败")

    flaky_channel = FeishuChannel("cli_check_fake", "fake_secret_value")
    flaky_dispatcher = flaky_channel.build_dispatcher(flaky_handler)
    flaky_event = make_event(message_id="om_flaky_1", event_id="evt_flaky_1")
    flaky_dispatcher._do_without_validation(payload(flaky_event))
    flaky_dispatcher._do_without_validation(payload(flaky_event))
    assert len(calls) == 2, f"回调失败后重推被去重吞掉: 回调 {len(calls)} 次"
    return "重复投递只回调一次；不同 id 放行；回调失败后释放去重键"


# ------------------------------------------------------------------- 7. 文本分段
def t_split_text() -> str:
    assert split_text("") == [], "空文本应返回空列表"
    assert split_text("短文本") == ["短文本"]

    # 无标点长文本 → 硬切，三段 4000/4000/1000
    raw = "a" * 9000
    chunks = split_text(raw)
    assert [len(c) for c in chunks] == [4000, 4000, 1000], f"硬切长度错误: {[len(c) for c in chunks]}"
    assert "".join(chunks) == raw, "分段后内容丢失"

    # 有标点 → 优先在句号后切，且每段不超限
    sentence = "这是一句完整的话。"
    long_text = sentence * 900
    chunks = split_text(long_text)
    assert len(chunks) >= 2
    assert all(len(c) <= DEFAULT_MAX_TEXT_CHARS for c in chunks), "存在超长分段"
    assert "".join(chunks) == long_text, "分段后内容丢失"
    assert all(c.endswith("。") for c in chunks[:-1]), "应优先在句末标点处切分"

    # 自定义 limit
    chunks = split_text("abcdefghij" * 3, limit=10)
    assert [len(c) for c in chunks] == [10, 10, 10]
    assert "".join(chunks) == "abcdefghij" * 3

    try:
        split_text("x", limit=0)
    except ValueError:
        pass
    else:
        raise AssertionError("limit<=0 应抛 ValueError")
    return f"空/短/硬切/标点切/自定义 limit 全部正确（默认 {DEFAULT_MAX_TEXT_CHARS} 字符）"


# ----------------------------------------------------- 8. 发送成功 + 自动分段
def t_send_success() -> str:
    channel, api = make_fake_channel()

    result = channel.send_text(chat_id="oc_check", text="你好", reply_to_message_id="om_user")
    assert result.ok and result.message_id == "om_out_1"
    assert len(api.calls) == 1 and api.calls[0]["kind"] == "reply"
    assert api.calls[0]["message_id"] == "om_user"
    assert json.loads(api.calls[0]["content"])["text"] == "你好"

    result = channel.send_text(chat_id="oc_check", text="主动发送")
    assert result.ok and result.message_id == "om_out_2"
    assert api.calls[1]["kind"] == "create"
    assert api.calls[1]["receive_id"] == "oc_check"
    assert api.calls[1]["receive_id_type"] == "chat_id"

    # 多段：第 1 段 reply，其余 create，且拼接后内容与原文一致
    channel, api = make_fake_channel()
    long_text = "b" * 9000
    result = channel.send_text(chat_id="oc_check", text=long_text, reply_to_message_id="om_user")
    assert result.ok
    assert [c["kind"] for c in api.calls] == ["reply", "create", "create"], api.calls
    assert "".join(json.loads(c["content"])["text"] for c in api.calls) == long_text
    assert result.message_id == "om_out_1", "返回的 message_id 应为第 1 段"

    # 只有 reply_to 没有 chat_id：所有段都 reply
    channel, api = make_fake_channel()
    result = channel.send_text(chat_id="", text="c" * 5000, reply_to_message_id="om_user")
    assert result.ok and [c["kind"] for c in api.calls] == ["reply", "reply"]

    # 入参保护：空文本 / 既没有 chat_id 也没有 reply_to
    channel, api = make_fake_channel()
    result = channel.send_text(chat_id="oc_check", text="  \n ")
    assert result.ok is False and result.error == "empty_text" and api.calls == []
    result = channel.send_text(chat_id="", text="hi")
    assert result.ok is False and result.error == "missing_chat_id"
    return "reply/create 路由、空文本与缺 chat_id 保护、多段拼接均正确"


# ------------------------------------------------------------- 9. 发送失败分支
def t_send_failures() -> str:
    # 飞书返回业务错误码（例如机器人可用范围未开）
    channel, _api = make_fake_channel(
        [_FakeResp(ok=False, code=230013, msg="Bot has NO availability to this user")]
    )
    result = channel.send_text(chat_id="oc_check", text="hello")
    assert isinstance(result, SendResult)
    assert result.ok is False and result.code == 230013
    assert "230013" in result.error, f"错误信息缺少 code: {result.error}"

    # 网络异常：不允许抛给调用方
    channel, _api = make_fake_channel([RuntimeError("network down")])
    result = channel.send_text(chat_id="oc_check", text="hello")
    assert result.ok is False and result.code is None
    assert "network down" in result.error

    # 多段中途失败：停止后续发送，保留第 1 段的 message_id
    channel, api = make_fake_channel(
        [
            _FakeResp(ok=True, message_id="om_first"),
            _FakeResp(ok=False, code=99991663, msg="rate limited"),
        ]
    )
    result = channel.send_text(chat_id="oc_check", text="d" * 9000)
    assert result.ok is False and result.code == 99991663
    assert result.message_id == "om_first"
    assert len(api.calls) == 2, f"失败后应停止后续分段，实际调用 {len(api.calls)} 次"
    return "业务错误码 / 网络异常 / 多段中途失败 均返回 SendResult(ok=False) 且不抛异常"


# ------------------------------------------------------- 10. 真实发送（显式门控）
def t_live_send() -> str:
    if os.getenv("FEISHU_LIVE") != "1":
        return "SKIP(feishu_live)：未设置 FEISHU_LIVE=1；全程使用假 API，不打扰手机"
    chat_id = os.getenv("FEISHU_LIVE_CHAT_ID", "")
    if not chat_id:
        return "SKIP(feishu_live)：FEISHU_LIVE=1 但未提供 FEISHU_LIVE_CHAT_ID"

    from config import settings  # 延迟导入：默认路径不需要 .env

    settings.require_feishu()
    channel = FeishuChannel(settings.feishu_app_id, settings.feishu_app_secret, settings.feishu_domain)
    result = channel.send_text(chat_id=chat_id, text=f"[check_channel] 验收探测 {ulid()}")
    assert result.ok, f"真实发送失败: code={result.code} error={result.error}"
    return f"真实发送成功 message_id={result.message_id}"


# --------------------------------------------------------------------------- 主流程
def main() -> int:
    print("=" * 72)
    print("M-CHANNEL 验收（飞书渠道；默认不访问网络、不发真实消息）")
    print(f"项目目录: {ROOT}")
    print(f"FEISHU_LIVE={os.getenv('FEISHU_LIVE', '0')}")
    print("注：失败分支打出的 Traceback 是契约要求的「吞异常并记日志」证据，不是验收失败。")
    print("=" * 72)

    check("1. Python / lark-oapi 依赖", t_dependencies)
    check("2. ConsoleChannel 收发闭环", t_console_channel)
    check("3. 飞书单聊事件解析", t_parse_p2p)
    check("4. 群聊 @ 清洗", t_parse_group_mentions)
    check("5. 机器人自身消息 / 非文本 / 坏内容过滤", t_parse_filters)
    check("6. 事件去重（含失败后重试）", t_dedupe)
    check("7. 文本分段", t_split_text)
    check("8. 发送成功与自动分段", t_send_success)
    check("9. 发送失败分支", t_send_failures)
    check("10. 真实发送（FEISHU_LIVE 门控）", t_live_send)

    failures = [name for name, ok, _ in RESULTS if not ok]
    print("=" * 72)
    print(f"结果：{len(RESULTS) - len(failures)}/{len(RESULTS)} 通过")
    for name in failures:
        print(f"  未通过 -> {name}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
