"""
配置模块。

使用 Pydantic Settings 从环境变量和 .env 文件加载配置。
支持飞书应用、LLM、调度器等组件的配置管理。
"""

from __future__ import annotations

from functools import cached_property
from pathlib import Path

from pydantic import Field, HttpUrl, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _csv(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


class Settings(BaseSettings):
    """
    应用配置类。

    从环境变量和 .env 文件加载配置，包含飞书应用、LLM、调度器等配置项。
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    feishu_app_id: str = Field(min_length=1)
    feishu_app_secret: str = Field(min_length=1)
    feishu_domain: HttpUrl = HttpUrl("https://open.feishu.cn")
    feishu_target_chat_id: str = ""
    feishu_allowed_user_ids: str = ""
    feishu_admin_user_ids: str = ""
    feishu_allowed_group_chat_ids: str = ""
    feishu_calendar_id: str = ""
    feishu_mcp_cli: str = ""
    feishu_verification_token: str = ""
    feishu_encrypt_key: str = ""

    public_base_url: HttpUrl = HttpUrl("http://127.0.0.1:45837")
    port: int = Field(default=45837, ge=1, le=65535)
    timezone: str = "Asia/Shanghai"

    llm_base_url: HttpUrl
    llm_api_key: str = Field(min_length=1)
    llm_model: str = Field(min_length=1)

    # LLM 超时配置（细粒度）
    llm_connect_timeout_seconds: float = Field(default=10.0, ge=1.0, le=30.0)
    llm_read_timeout_seconds: float = Field(default=75.0, ge=10.0, le=180.0)
    llm_write_timeout_seconds: float = Field(default=15.0, ge=5.0, le=60.0)
    llm_pool_timeout_seconds: float = Field(default=5.0, ge=1.0, le=30.0)

    # LLM 重试配置
    llm_max_retries: int = Field(default=1, ge=0, le=2)

    # LLM 输出 Token 限制（按阶段）
    llm_decision_max_tokens: int = Field(default=1200, ge=200, le=2000)
    llm_final_max_tokens: int = Field(default=4096, ge=500, le=8000)
    llm_document_max_tokens: int = Field(default=12000, ge=1000, le=16000)

    # Agent 运行预算
    agent_run_timeout_seconds: int = Field(default=150, ge=30, le=600)
    agent_step_timeout_seconds: int = Field(default=85, ge=20, le=120)
    agent_tool_timeout_seconds: int = Field(default=30, ge=10, le=60)

    # Agent 请求预算
    agent_max_llm_requests: int = Field(default=6, ge=1, le=12)
    agent_max_tool_calls: int = Field(default=6, ge=1, le=30)
    agent_max_steps: int = Field(default=8, ge=1, le=15)
    agent_max_consecutive_errors: int = Field(default=2, ge=1, le=5)

    # Agent 预留时间
    agent_finish_reserve_seconds: int = Field(default=10, ge=5, le=30)

    # LLM 连接池配置
    llm_max_connections: int = Field(default=50, ge=10, le=100)
    llm_max_keepalive_connections: int = Field(default=20, ge=5, le=50)
    llm_keepalive_expiry_seconds: float = Field(default=30.0, ge=10.0, le=60.0)

    morning_cron: str = "45 10 * * *"
    evening_cron: str = "30 23 * * *"
    scheduler_poll_seconds: int = Field(default=15, ge=1, le=300)
    database_path: Path = Path(".data/song-agent.db")
    # Legacy JSON is read once during SQLite initialization and is never overwritten.
    data_file: Path = Path(".data/state.json")
    processed_event_retention_days: int = Field(default=30, ge=1, le=365)
    pending_action_ttl_seconds: int = Field(default=1800, ge=60, le=86400)
    song_agent_token_active_key_version: int = Field(default=1, ge=1, le=4)
    song_agent_token_key_v1: SecretStr | None = None
    song_agent_token_key_v2: SecretStr | None = None
    song_agent_token_key_v3: SecretStr | None = None
    song_agent_token_key_v4: SecretStr | None = None
    log_level: str = "INFO"

    @model_validator(mode="after")
    def normalize_log_level(self) -> Settings:
        self.log_level = self.log_level.upper()
        return self

    @cached_property
    def admin_user_ids(self) -> set[str]:
        # Backward compatible: the former allow-list becomes the initial group-binding admin list.
        return _csv(self.feishu_admin_user_ids) or _csv(self.feishu_allowed_user_ids)

    @cached_property
    def allowed_group_chat_ids(self) -> set[str]:
        return _csv(self.feishu_allowed_group_chat_ids)

    @property
    def domain(self) -> str:
        return str(self.feishu_domain).rstrip("/")

    @property
    def base_url(self) -> str:
        return str(self.public_base_url).rstrip("/")

    @property
    def llm_url(self) -> str:
        return str(self.llm_base_url).rstrip("/")

    @property
    def required_oauth_scopes(self) -> tuple[str, ...]:
        return (
            "calendar:calendar",
            "calendar:calendar:readonly",
            "docx:document",
            "drive:drive",
            "search:docs:read",
        )

    @property
    def token_encryption_keys(self) -> dict[int, str]:
        configured = (
            self.song_agent_token_key_v1,
            self.song_agent_token_key_v2,
            self.song_agent_token_key_v3,
            self.song_agent_token_key_v4,
        )
        return {
            version: key.get_secret_value()
            for version, key in enumerate(configured, start=1)
            if key is not None and key.get_secret_value()
        }

    def resolve_mcp_cli(self) -> Path | None:
        if self.feishu_mcp_cli:
            configured = Path(self.feishu_mcp_cli).expanduser().resolve()
            if configured.is_file():
                return configured
        local = Path("node_modules/@larksuiteoapi/lark-mcp/dist/cli.js").resolve()
        if local.is_file():
            return local
        return None
