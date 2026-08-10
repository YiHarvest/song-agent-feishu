"""Core configuration and isolated adapter/module configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import Field, HttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

_COMMON = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


class CoreSettings(BaseSettings):
    model_config = SettingsConfigDict(**_COMMON, env_prefix="SONG_AGENT_")

    database_path: Path = Path(".data/song-agent-v1.db")
    master_key: SecretStr
    enabled_modules: str = (
        "conversation,memory,agent,plan,delivery,system"
    )
    external_plugins_enabled: bool = False
    external_plugin_allowlist: str = ""
    max_execution_timeout_seconds: float = Field(default=180.0, ge=1, le=600)
    confirm_all_writes: bool = False
    log_level: str = "INFO"
    public_base_url: HttpUrl = HttpUrl("http://127.0.0.1:45837")

    @property
    def module_ids(self) -> frozenset[str]:
        return frozenset(item.strip() for item in self.enabled_modules.split(",") if item.strip())

    @property
    def plugin_allowlist(self) -> frozenset[str]:
        return frozenset(
            item.strip() for item in self.external_plugin_allowlist.split(",") if item.strip()
        )


class ApiSettings(BaseSettings):
    model_config = SettingsConfigDict(**_COMMON, env_prefix="SONG_AGENT_API_")

    key: SecretStr
    key_name: str = "default"
    default_tenant: str = "default"
    principal_id: str = "api-client"
    model_id: str = "song-agent-v1"
    max_messages: int = Field(default=100, ge=1, le=1000)
    max_total_chars: int = Field(default=200_000, ge=1, le=2_000_000)
    rate_limit_per_minute: int = Field(default=60, ge=1, le=10_000)


class LlmSettings(BaseSettings):
    model_config = SettingsConfigDict(**_COMMON, env_prefix="LLM_")

    base_url: HttpUrl
    api_key: SecretStr
    model: str = Field(min_length=1)
    connect_timeout_seconds: float = Field(default=10, ge=1, le=60)
    read_timeout_seconds: float = Field(default=90, ge=5, le=600)
    max_retries: int = Field(default=1, ge=0, le=3)


class FeishuSettings(BaseSettings):
    model_config = SettingsConfigDict(**_COMMON, env_prefix="FEISHU_")

    channel_enabled: bool = False
    workspace_enabled: bool = False
    app_id: str = ""
    app_secret: SecretStr | None = None
    domain: HttpUrl = HttpUrl("https://open.feishu.cn")
    verification_token: SecretStr | None = None
    encrypt_key: SecretStr | None = None
    calendar_id: str = ""
    granted_permissions: str = (
        "calendar.read,calendar.write,task.read,task.write,"
        "document.read,document.write"
    )

    @property
    def permissions(self) -> frozenset[str]:
        return frozenset(
            item.strip() for item in self.granted_permissions.split(",") if item.strip()
        )


class AttachmentSettings(BaseSettings):
    model_config = SettingsConfigDict(**_COMMON, env_prefix="SONG_AGENT_ATTACHMENT_")

    directory: Path = Path(".data/attachments-v1")
    max_bytes: int = Field(default=150 * 1024 * 1024, ge=1024)
    ttl_seconds: int = Field(default=2_592_000, ge=3600)
    vision_base_url: HttpUrl | None = None
    vision_api_key: SecretStr | None = None
    vision_model: str = ""
    asr_url: HttpUrl | None = None
    document_parser_url: HttpUrl | None = None


class SchedulerSettings(BaseSettings):
    model_config = SettingsConfigDict(**_COMMON, env_prefix="SONG_AGENT_SCHEDULER_")

    lease_seconds: int = Field(default=30, ge=5, le=600)


class SearchSettings(BaseSettings):
    model_config = SettingsConfigDict(**_COMMON, env_prefix="SONG_AGENT_SEARCH_")

    enabled: bool = False
    provider: str = "tavily"
    tavily_api_key: SecretStr | None = None
    tavily_endpoint: HttpUrl = HttpUrl("https://api.tavily.com/search")


@dataclass(frozen=True, slots=True)
class Settings:
    core: CoreSettings
    api: ApiSettings
    llm: LlmSettings
    feishu: FeishuSettings
    attachments: AttachmentSettings
    scheduler: SchedulerSettings
    search: SearchSettings


def load_settings() -> Settings:
    return Settings(
        core=CoreSettings(),
        api=ApiSettings(),
        llm=LlmSettings(),
        feishu=FeishuSettings(),
        attachments=AttachmentSettings(),
        scheduler=SchedulerSettings(),
        search=SearchSettings(),
    )
