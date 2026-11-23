"""全局配置：只从环境变量 / `.env` 读取，代码里不出现任何真实密钥。

**本文件由主线维护**。新增配置项走 `docs/ENGINEERING_CONTRACT.md` §11 的变更流程。
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent

# 凭证只从既有 .env 读取；可用 ASSISTANT_ENV_FILE 指向仓库外的凭证文件，
# 避免把真实密钥复制进新仓库（复制本身也违反"不提交密钥"的约束）。
ENV_FILE = Path(os.getenv("ASSISTANT_ENV_FILE", str(BASE_DIR / ".env")))

_WIN_ENV_VAR = re.compile(r"%([A-Za-z_][A-Za-z0-9_]*)%")


def _expand_path(value: Any) -> Any:
    """展开路径里的 `~` / `%USERPROFILE%` / `%VAR%`，返回 `Path`。

    pydantic 对 `Path` 字段只做 `Path(value)`，**不会**展开变量。如果 `.env`
    里写 `%USERPROFILE%\\.assistant`，会被原样当成一个名为 `%USERPROFILE%`
    的相对目录，在当前工作目录下静默建错。这里统一展开；变量未定义时直接
    报错，不让错误路径流进运行期。
    """
    if value is None:
        return None
    if not isinstance(value, (str, Path)):
        return value
    text = os.path.expanduser(str(value).strip())
    missing: list[str] = []

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in os.environ:
            missing.append(name)
            return match.group(0)
        return os.environ[name]

    text = _WIN_ENV_VAR.sub(_sub, text)
    if missing:
        raise ValueError(
            f"路径中的环境变量未定义: {', '.join(sorted(set(missing)))}（原值 {value!r}）"
        )
    return Path(text)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------- 渠道 ----------
    channel_kind: str = "feishu"          # "feishu" | "console"

    # ---------- 飞书 ----------
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_domain: str = "https://open.feishu.cn"
    # 发文件上限（2026-11 用真实应用凭据实测）：
    # - im/v1/images：10 MiB 通过、10 MiB+1 报 234006「The file size exceed the max value」；
    # - im/v1/files ：27,000,000 字节通过、27,500,000 起报 9499/2200，取 25 MiB 留安全边际。
    # 数字会随平台调整，改这一处即可；提示词只讲"超限了怎么办"。
    feishu_image_max_bytes: int = 10 * 1024 * 1024
    feishu_file_max_bytes: int = 25 * 1024 * 1024

    # ---------- Redis ----------
    redis_url: str = "redis://127.0.0.1:6379/0"
    redis_socket_timeout_s: float = 10.0

    # ---------- Postgres（checkpoint 真相） ----------
    postgres_dsn: str = ""
    postgres_pool_min: int = 1
    postgres_pool_max: int = 8

    # ---------- DeepSeek（OpenAI 兼容） ----------
    llm_base_url: str = "https://api.deepseek.com"
    llm_api_key: str = ""
    llm_model: str = "deepseek-flash"
    # 文档 §10.2 的风险：思考模式 + 工具调用需要回传 reasoning_content。
    # v1 先关闭思考作为确定性取舍；真 key 验证后再打开（记录在契约 §14）。
    llm_thinking_disabled: bool = True
    # 思考档位（OpenAI 兼容的 `reasoning_effort`）；留空 = 请求体不带该字段。
    # 这是"思考开着但压低档位"的唯一开关，与 llm_thinking_disabled 互斥：
    # 关闭思考时只发 thinking.type=disabled，不再叠加 reasoning_effort。
    llm_reasoning_effort: str = "low"
    # 当前模型是否支持 image_url 内容块；False 时由装配层 VisionAdapter 降级为文本。
    # 主 agent 模型（GLM 系）支持图片理解，默认 True；换纯文本模型时才需要在 .env 关掉。
    llm_supports_vision: bool = True

    # ---------- DeepSeek Web 工具（契约 §7.7） ----------
    # 搜索/抓取专用凭证，**只用于 /v1/tools/search 与 /v1/tools/fetch**。
    # 与 llm_api_key 物理隔离：两边都不允许 fallback 到对方（代码层面禁止互读）。
    # 未配置时 WebSearch/WebFetch 返回结构化 not_configured，不做任何降级。
    deepseek_search_api_key: str = ""
    search_api_base_url: str = "https://api.deepseek.com"
    # count / need_content 的默认值由 `WebSearch` 签名钉死（5 / False），
    # 这里**不再**保留同义字段，避免出现两套默认值（契约 §7.7）。
    web_search_timeout_s: float = 30.0

    # ---------- 运行目录（与代码分离，文档 §2.3） ----------
    data_dir: Path = Path.home() / ".assistant"
    workspace_root: Path = Path.home() / "assistant-workspace"
    # worker 进程入口；默认用当前解释器（必须是 conda env `feishu`）
    worker_python: str = ""

    @field_validator("data_dir", "workspace_root", mode="before")
    @classmethod
    def _expand_run_paths(cls, value: Any) -> Any:
        """允许 `.env` 用 `%USERPROFILE%\\.assistant` 这类写法。"""
        return _expand_path(value)

    # ---------- Router 运行时 ----------
    router_thread_id: str = "router:main"
    router_idle_exit_s: int = 60
    router_read_block_ms: int = 5000
    batch_debounce_ms: int = 200
    batch_max: int = 10
    # 单次 invoke 的图步数上限。300 会在"干完活再自查"这类长尾上撞限
    # （活已做完但 GraphRecursionError 把状态记成 error），故放宽到 1000。
    router_recursion_limit: int = 1000
    worker_recursion_limit: int = 1000
    # worker 的 WebSearch 默认值（比 router 更保守，避免工具输出撑爆上下文）
    worker_web_default_count: int = 3
    worker_web_content_chars: int = 2048
    # 文档 §7.6：router 绝不跑长命令；worker 可以长跑
    router_shell_timeout_ms: int = 30000
    worker_shell_timeout_ms: int = 600000
    # 文档 §7.3.4：Shell/WebFetch 长输出阈值
    tool_output_max_lines: int = 2000
    tool_output_max_bytes: int = 64 * 1024
    tool_output_head_lines: int = 30
    tool_output_tail_lines: int = 60

    # ---------- Worker 三级上下文压缩（agents/compact.py，对齐 Claude Code 级联） ----------
    # 所有阈值按 context_window_tokens 的比例计算，随窗口缩放，不写死绝对值。
    # 有效上下文窗口（比例基准）：30w 有效窗口 × 20% = 6w 的 L2 单批摘要窗口。
    # 注意：30w 不是模型真实窗口（deepseek-v4.1-flash 为 1M），而是**刻意压低**的
    # worker 压缩预算——agent loop 每轮重发完整 messages，上下文越长，单轮与整任务的
    # prompt 成本按轮数放大。30w × 0.85 = 255k 触发、× 0.60 = 180k 收口是成本闸门，
    # 不是能力上限。调大只会让超长任务更贵，不会让它们更能干。
    context_window_tokens: int = 300_000
    # 触发水位：用量达到窗口 × 0.85 时开始压缩（L1 → L2 → L3 递进）
    compact_trigger_ratio: float = 0.85
    # 目标水位：压缩到窗口 × 0.60 为止
    compact_target_ratio: float = 0.60
    # L2 单批摘要窗口 = 窗口 × 20%（且至少覆盖 current - target 的超额量）
    compact_l2_batch_ratio: float = 0.20
    # 最近 N 轮 AI 交互不压缩（保护区，不拆 tool_call / ToolMessage 配对）
    compact_protect_rounds: int = 3
    # L1 截断后的预览长度（字符）
    compact_preview_chars: int = 2000
    # 工具输出短于此长度不压（字符）
    compact_min_compress_chars: int = 800
    # L2 摘要目标长度（字符）
    compact_summary_chars: int = 3000
    # L3 全量摘要目标长度（字符）
    compact_l3_summary_chars: int = 2000

    # 文档 §3.6：上下文预算与折叠（router / worker 共用同一组阈值）
    context_soft_tokens: int = 256_000
    context_hard_tokens: int = 320_000
    context_keep_recent_messages: int = 8
    context_keep_recent_tokens: int = 40_000
    # 硬预算兜底：窗口内单条 ToolMessage 正文最多保留的字符数
    context_tool_message_max_chars: int = 4000

    # 文档 §5.6：router → worker 指令通道（worker 侧轮询间隔与积压上限）
    worker_inbox_poll_s: float = 0.3
    worker_inbox_max: int = 20
    # 文档 §5.5：StopCommand 第一级（礼）等待 worker 回写 level=2 的时长
    stop_graceful_wait_s: float = 2.0

    # ---------- supervisor（host 常驻线程） ----------
    schedule_scan_interval_s: int = 5
    watch_scan_interval_s: int = 300
    stall_minutes: int = 15
    spawning_lock_ttl_s: int = 30
    heartbeat_ttl_s: int = 10
    dedupe_ttl_s: int = 6 * 3600
    reply_idem_ttl_s: int = 86400

    # ---------- 观测 ----------
    log_level: str = "INFO"
    # 日志轮转（log_rotation.py）：单文件达到阈值就切成 .1…N，旧的删除。
    # gateway 进程有常驻线程按 check_s 周期扫描；gateway.ps1 start 也会先扫一遍。
    log_rotate_enabled: bool = True
    log_rotate_max_bytes: int = 20 * 1024 * 1024
    log_rotate_backups: int = 5
    log_rotate_check_s: int = 60

    # ---------- 文件区域（§7.1 / B3）----------
    # 可玩耍区：读写删全放开的唯一区域。留空 = workspace_root；
    # 把助手整体搬到 D 盘时配成 `D:\agent`（或 `D:\`），所有工具与 Shell cwd 同步生效。
    play_root: str = ""
    # 只读区：默认用户主目录下的 Desktop / Downloads / Documents；可增删（相对名或绝对路径）。
    # 写/删一律拒绝，只有 Read/Glob/Grep 能进去。
    readonly_roots: list[str] = []

    # ---------- CUA（§8，纯视觉单屏） ----------
    # 浏览器内核**唯一**配置点：默认 145；切 158 只改这一行。
    # 备选：C:\Users\34897\Documents\Trae-CN\temp_20261108_06\cua-browsers\chromium-1254\chrome-win64\chrome.exe
    cua_browser_exe: str = r"D:\repo\chrome-win64\chrome.exe"
    # 任务开始前是否自动拉起内核；桌面类任务不需要，默认关。
    cua_browser_autostart: bool = False
    # 截图保留窗口（张）：1 = 只有当前一张；越大前后对比视野越长。
    # 上限由注入层钳制（agents.vision.INJECT_MAX_IMAGES=6），超限按上限钳住并告警。
    cua_screenshot_window: int = 3
    # `act` 单批上限：批内不看图，批越长越容易走偏（§8.9.4 建议 3，未验证）。
    cua_max_batch: int = 3
    # 单任务物理动作预算（点击/键入/滚动/等待都算），到顶即停。
    cua_max_actions: int = 100
    cua_wait_max_ms: int = 5000

    # ---------- 派生路径 ----------
    @property
    def play_root_path(self) -> Path:
        """可玩耍区（真实绝对路径）。"""
        raw = (self.play_root or "").strip()
        return Path(raw).expanduser().resolve() if raw else self.workspace_root

    @property
    def readonly_root_paths(self) -> list[Path]:
        """只读区列表（真实绝对路径）；飞书附件目录由调用方另外追加。"""
        items = self.readonly_roots or ["Desktop", "Downloads", "Documents"]
        roots: list[Path] = []
        for item in items:
            path = Path(item).expanduser()
            if not path.is_absolute():
                path = Path.home() / item
            try:
                roots.append(path.resolve())
            except OSError:
                roots.append(path.absolute())
        return roots

    @property
    def router_dir(self) -> Path:
        return self.data_dir / "router"

    @property
    def memory_dir(self) -> Path:
        return self.router_dir / "memory"

    @property
    def tool_outputs_dir(self) -> Path:
        return self.data_dir / "tool_outputs"

    @property
    def inbox_dir(self) -> Path:
        """飞书图片/文件的落盘目录（§6.2，gateway 侧使用）。"""
        return self.data_dir / "inbox"

    @property
    def workers_dir(self) -> Path:
        return self.data_dir / "workers"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "db" / "assistant.db"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def state_dir(self) -> Path:
        return self.data_dir / "state"

    def ensure_dirs(self) -> None:
        for path in (
            self.router_dir,
            self.memory_dir,
            self.tool_outputs_dir,
            self.inbox_dir,
            self.workers_dir,
            self.db_path.parent,
            self.logs_dir,
            self.state_dir,
            self.workspace_root,
        ):
            path.mkdir(parents=True, exist_ok=True)

    # ---------- 校验 ----------
    def require_feishu(self) -> None:
        missing = [
            name
            for name, value in (
                ("FEISHU_APP_ID", self.feishu_app_id),
                ("FEISHU_APP_SECRET", self.feishu_app_secret),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                "缺少飞书凭证: " + ", ".join(missing) + "。请复制 .env.example 为 .env 并填写。"
            )

    def pg_dsn(self) -> str:
        if not self.postgres_dsn:
            raise RuntimeError(
                "缺少 POSTGRES_DSN。请在 .env 中填写，例如 "
                "postgresql://user:password@127.0.0.1:5432/dbname"
            )
        return self.postgres_dsn

    def python_exe(self) -> str:
        return self.worker_python or sys.executable


settings = Settings()
