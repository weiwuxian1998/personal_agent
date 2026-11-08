"""全局配置：只从环境变量 / `.env` 读取，代码里不出现任何真实密钥。

**本文件由主线维护**。新增配置项走 `docs/ENGINEERING_CONTRACT.md` §11 的变更流程。
"""

from __future__ import annotations

import sys
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------- 飞书 ----------
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_domain: str = "https://open.feishu.cn"

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
    # 当前模型是否支持 image_url 内容块；False 时由装配层 VisionAdapter 降级为文本。
    # deepseek-flash 为文本模型，v1 默认 False。
    llm_supports_vision: bool = False

    # ---------- 运行目录（与代码分离，文档 §2.3） ----------
    data_dir: Path = Path.home() / ".assistant"
    workspace_root: Path = Path.home() / "assistant-workspace"
    # worker 进程入口；默认用当前解释器（必须是 conda env `feishu`）
    worker_python: str = ""

    # ---------- Router 运行时 ----------
    router_thread_id: str = "router:main"
    router_idle_exit_s: int = 60
    router_read_block_ms: int = 5000
    batch_debounce_ms: int = 200
    batch_max: int = 10
    router_recursion_limit: int = 30
    worker_recursion_limit: int = 40
    # 文档 §7.6：router 绝不跑长命令；worker 可以长跑
    router_shell_timeout_ms: int = 30000
    worker_shell_timeout_ms: int = 600000
    # 文档 §7.3.4：Shell/WebFetch 长输出阈值
    tool_output_max_lines: int = 2000
    tool_output_max_bytes: int = 64 * 1024
    tool_output_head_lines: int = 30
    tool_output_tail_lines: int = 60

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

    # ---------- 派生路径 ----------
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
