"""按配置构造渠道实例（只由 gateway 使用）。

为什么需要这个文件：gateway 需要 `run_forever` 收消息，也需要 `send_text`
发送 router 写入 outbox 的回复；选中哪一种渠道只在这里实现一次，
避免 `channel_kind` 与命令行参数的优先级出现两份实现。

**router 进程不构造渠道**（契约 §9.1）：ReplyUser 只写 outbox Stream，
由 gateway 的 OutboxSender 调 `send_text`。因此 router 冷启动不会
`import lark_oapi`（实测该导入 47–51s）。

注意：`lark_oapi` 的导入非常慢（会加载几千个 API 模型模块，实测数十秒）。
因此这里**不在模块顶层导入 `channel.feishu`**，只在真正选择飞书渠道时才导入；
控制台模式（联调）与 worker 进程都不该为飞书的导入付出启动代价。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from config import Settings

if TYPE_CHECKING:
    from channel.base import Channel


def build_channel(settings: Settings) -> "Channel":
    """按 `settings.channel_kind` 返回渠道；未知取值直接失败，不静默降级。"""
    kind = (settings.channel_kind or "feishu").strip().lower()
    if kind == "console":
        from channel.console import ConsoleChannel

        return ConsoleChannel()
    if kind == "feishu":
        from channel.feishu import FeishuChannel

        settings.require_feishu()
        return FeishuChannel(
            settings.feishu_app_id,
            settings.feishu_app_secret,
            settings.feishu_domain,
            inbox_dir=str(settings.inbox_dir),
            image_max_bytes=settings.feishu_image_max_bytes,
            file_max_bytes=settings.feishu_file_max_bytes,
        )
    raise ValueError(f"未知 channel_kind: {settings.channel_kind!r}（支持 feishu/console）")
