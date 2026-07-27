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

    # Search Engine MCP API Keys
    searxng_base_url: str = ""
    talordata_api_key: str = ""
    ydc_api_key: str = ""
    tavily_api_key: str = ""

    public_base_url: HttpUrl = HttpUrl("http://0.0.0.0:45837")
    port: int = Field(default=45837, ge=1, le=65535)
    timezone: str = "Asia/Shanghai"

    llm_base_url: HttpUrl
    llm_api_key: str = Field(min_length=1)
    llm_model: str = Field(min_length=1)

    # 对外 OpenAI 兼容 API（仅文本）。
    song_agent_api_enabled: bool = False
    song_agent_api_model_id: str = "song-agent-2.1"
    song_agent_api_key_name: str = "default"
    song_agent_api_key: SecretStr | None = None
    song_agent_api_default_tenant: str = "default"
    song_agent_api_app_id: str = "song-agent-api"
    song_agent_api_rate_limit_per_minute: int = Field(default=60, ge=1, le=10_000)
    song_agent_api_max_messages: int = Field(default=100, ge=1, le=1000)
    song_agent_api_max_message_chars: int = Field(default=50_000, ge=1, le=1_000_000)
    song_agent_api_max_total_chars: int = Field(default=200_000, ge=1, le=2_000_000)
    song_agent_api_sync_timeout_seconds: int = Field(default=180, ge=10, le=600)
    song_agent_api_idempotency_ttl_seconds: int = Field(default=86_400, ge=60, le=604_800)
    song_agent_api_binding_code_ttl_seconds: int = Field(default=600, ge=60, le=3600)
    song_agent_api_health_details_enabled: bool = False

    # 飞书附件、图片理解、语音识别和文件解析。
    song_agent_attachments_enabled: bool = False
    song_agent_attachment_dir: Path = Path(".data/attachments")
    song_agent_attachment_temp_dir: Path = Path(".data/tmp")
    song_agent_attachment_ttl_seconds: int = Field(default=2_592_000, ge=3600)
    song_agent_attachment_max_files_per_message: int = Field(default=5, ge=1, le=20)
    song_agent_attachment_max_total_mb_per_message: int = Field(default=150, ge=1, le=1000)
    song_agent_attachment_download_timeout_seconds: int = Field(default=90, ge=5, le=600)

    song_agent_vision_enabled: bool = False
    song_agent_vision_base_url: HttpUrl = HttpUrl("https://api.moonshot.cn/v1")
    song_agent_vision_api_key: SecretStr | None = None
    song_agent_vision_model: str = "kimi-k2.6"
    song_agent_vision_connect_timeout_seconds: float = Field(default=10, ge=1, le=60)
    song_agent_vision_read_timeout_seconds: float = Field(default=45, ge=10, le=600)
    song_agent_vision_max_retries: int = Field(default=0, ge=0, le=2)
    song_agent_vision_max_tokens: int = Field(default=1600, ge=200, le=4000)
    song_agent_vision_thinking_enabled: bool = False
    song_agent_vision_max_image_mb: int = Field(default=20, ge=1, le=100)
    song_agent_vision_max_images_per_message: int = Field(default=4, ge=1, le=20)

    song_agent_asr_enabled: bool = False
    song_agent_asr_base_url: HttpUrl = HttpUrl("http://183.147.142.111:25570")
    song_agent_asr_path: str = "/api/v1/asr"
    song_agent_asr_default_language: str = "auto"
    song_agent_asr_connect_timeout_seconds: float = Field(default=10, ge=1, le=60)
    song_agent_asr_read_timeout_seconds: float = Field(default=300, ge=10, le=900)
    song_agent_asr_max_audio_mb: int = Field(default=100, ge=1, le=500)

    song_agent_document_parser_enabled: bool = False
    song_agent_document_parser_provider: str = "mineru_vl"
    song_agent_document_parse_timeout_seconds: int = Field(default=300, ge=10, le=3600)
    song_agent_document_max_file_mb: int = Field(default=50, ge=1, le=500)
    song_agent_document_max_context_chars: int = Field(default=4000, ge=500, le=20_000)
    song_agent_document_max_preview_chars: int = Field(default=2000, ge=200, le=10_000)
    song_agent_mineru_vl_base_url: HttpUrl = HttpUrl("http://183.147.142.111:63359/v1")
    song_agent_mineru_vl_model_name: str = ""
    song_agent_mineru_vl_server_headers: str = "{}"
    song_agent_mineru_vl_connect_timeout_seconds: int = Field(default=10, ge=1, le=60)
    song_agent_mineru_vl_read_timeout_seconds: int = Field(default=300, ge=10, le=900)
    song_agent_mineru_vl_pdf_scale: float = Field(default=2.0, ge=1.0, le=4.0)
    song_agent_mineru_vl_max_pages: int = Field(default=100, ge=1, le=500)
    song_agent_mineru_vl_page_concurrency: int = Field(default=2, ge=1, le=8)
    song_agent_mineru_vl_region_concurrency: int = Field(default=4, ge=1, le=32)
    song_agent_mineru_vl_max_retries: int = Field(default=1, ge=0, le=3)

    # LLM 超时配置（细粒度）
    llm_connect_timeout_seconds: float = Field(default=10.0, ge=1.0, le=30.0)
    llm_read_timeout_seconds: float = Field(default=60.0, ge=10.0, le=180.0)
    llm_write_timeout_seconds: float = Field(default=15.0, ge=5.0, le=60.0)
    llm_pool_timeout_seconds: float = Field(default=5.0, ge=1.0, le=30.0)

    # LLM 重试配置
    llm_max_retries: int = Field(default=1, ge=0, le=2)

    # LLM 结构化输出模式
    # - "json_schema": OpenAI Structured Outputs，严格保证输出符合 Schema
    # - "json_object": 宽松模式，只保证输出是有效 JSON
    # - "none": 禁用结构化输出，完全依赖提示词
    # 注意：只有 OpenAI 和部分兼容 API（如 SiliconFlow）支持 json_schema
    llm_structured_output_mode: str = Field(default="json_object")

    # LLM 输出 Token 限制（按阶段）
    llm_decision_max_tokens: int = Field(default=1200, ge=200, le=2000)
    llm_final_max_tokens: int = Field(default=4096, ge=500, le=8000)
    llm_document_max_tokens: int = Field(default=12000, ge=1000, le=16000)

    # Agent 运行预算
    # 注意：agent_step_timeout 必须大于 llm_read_timeout * (1 + llm_max_retries)
    # 当前：150s > 60s * 2 = 120s，留有 30s 缓冲
    agent_run_timeout_seconds: int = Field(default=150, ge=30, le=600)
    agent_step_timeout_seconds: int = Field(default=150, ge=20, le=180)
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
    def agent_api_configured(self) -> bool:
        return bool(
            self.song_agent_api_enabled
            and self.song_agent_api_key is not None
            and self.song_agent_api_key.get_secret_value()
            and self.song_agent_api_model_id
        )

    @property
    def required_oauth_scopes(self) -> tuple[str, ...]:
        return (
            "calendar:calendar",
            "calendar:calendar:readonly",
            "calendar:calendar.event:create",
            "calendar:calendar.event:read",
            "task:task:read",
            "task:task:write",
            "docx:document",
            "drive:drive",
            "search:docs:read",
            "im:message:readonly",
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
