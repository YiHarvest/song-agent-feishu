from __future__ import annotations

import base64
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from song_agent.config import (
    ApiSettings,
    AttachmentSettings,
    CoreSettings,
    FeishuSettings,
    LlmSettings,
    SchedulerSettings,
    SearchSettings,
    Settings,
)


@pytest.fixture
def database_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "song-agent-v1.db"
    monkeypatch.setenv("SONG_AGENT_DATABASE_PATH", str(path))
    command.upgrade(Config("alembic.ini"), "head")
    return path


@pytest.fixture
def settings(database_path: Path, tmp_path: Path) -> Settings:
    master_key = base64.urlsafe_b64encode(b"x" * 32).decode()
    return Settings(
        core=CoreSettings(database_path=database_path, master_key=master_key),
        api=ApiSettings(key="test-api-key"),
        llm=LlmSettings(
            base_url="https://example.invalid/v1",
            api_key="test-llm-key",
            model="test-model",
            max_retries=0,
        ),
        feishu=FeishuSettings(),
        attachments=AttachmentSettings(directory=tmp_path / "attachments"),
        scheduler=SchedulerSettings(),
        search=SearchSettings(),
    )
