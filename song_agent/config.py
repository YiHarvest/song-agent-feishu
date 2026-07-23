"""
配置模块。

使用 Pydantic Settings 从环境变量和 .env 文件加载配置。
支持飞书应用、LLM、调度器等组件的配置管理。
"""

from __future__ import annotations

from functools import cached_property
from pathlib import Path

from pydantic import Field, HttpUrl, model_validator
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

    public_base_url: HttpUrl = HttpUrl("http://127.0.0.1:45837")
    port: int = Field(default=45837, ge=1, le=65535)
    timezone: str = "Asia/Shanghai"

    llm_base_url: HttpUrl
    llm_api_key: str = Field(min_length=1)
    llm_model: str = Field(min_length=1)

    morning_cron: str = "45 10 * * *"
    evening_cron: str = "30 23 * * *"
    data_file: Path = Path(".data/state.json")
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
        return ("calendar:calendar", "calendar:calendar:readonly", "docx:document", "drive:drive")

    def resolve_mcp_cli(self) -> Path | None:
        if self.feishu_mcp_cli:
            configured = Path(self.feishu_mcp_cli).expanduser().resolve()
            if configured.is_file():
                return configured
        local = Path("node_modules/@larksuiteoapi/lark-mcp/dist/cli.js").resolve()
        if local.is_file():
            return local
        return None
