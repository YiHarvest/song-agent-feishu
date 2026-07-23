import json
import time
from pathlib import Path

import pytest

from song_agent.models import DailyRecord, DocumentBinding, OAuthToken, PlanTask
from song_agent.store import JsonStore


@pytest.mark.asyncio
async def test_records_are_isolated_by_chat_user_and_date(tmp_path: Path) -> None:
    store = JsonStore(tmp_path / "state.json")
    await store.initialize()
    for user_id, title in (("user-a", "A 的提醒"), ("user-b", "B 的提醒")):
        record = DailyRecord(
            key=store.record_key("group", user_id, "2026-07-22"),
            date="2026-07-22",
            chat_id="group",
            user_id=user_id,
            plan_status="draft",
            tasks=[PlanTask(id="A1", priority="A", title=title, start_time="20:00")],
            created_at="now",
            updated_at="now",
        )
        await store.save_record(record)
    assert store.get_record("group", "user-a", "2026-07-22").tasks[0].title == "A 的提醒"
    assert store.get_record("group", "user-b", "2026-07-22").tasks[0].title == "B 的提醒"


@pytest.mark.asyncio
async def test_oauth_tokens_are_isolated_per_user(tmp_path: Path) -> None:
    store = JsonStore(tmp_path / "state.json")
    await store.initialize()
    now = int(time.time() * 1000)
    for user_id in ("user-a", "user-b"):
        await store.save_token(
            OAuthToken(
                user_id=user_id,
                access_token=f"access-{user_id}",
                refresh_token=f"refresh-{user_id}",
                expires_at=now + 1_000_000,
                refresh_expires_at=now + 2_000_000,
                scope="calendar:calendar drive:drive docx:document",
            )
        )
    assert store.get_token("user-a").access_token == "access-user-a"
    assert store.get_token("user-b").access_token == "access-user-b"


@pytest.mark.asyncio
async def test_document_bindings_are_isolated_by_chat_and_user(tmp_path: Path) -> None:
    store = JsonStore(tmp_path / "state.json")
    await store.initialize()
    await store.save_document_binding(
        DocumentBinding(
            chat_id="chat-a",
            user_id="user-a",
            title="每日计划",
            token="doc-token",
            url="https://feishu.cn/docx/doc-token",
        )
    )
    assert store.get_document_binding("chat-a", "user-a").token == "doc-token"
    assert store.get_document_binding("chat-a", "user-b") is None


@pytest.mark.asyncio
async def test_version_one_state_migrates_without_reusing_old_shared_record(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "records": {"old-shared-record": {"unsafe": True}},
                "auth": {},
                "processedMessageIds": ["message-1"],
                "binding": {"chatId": "p2p-chat", "userId": "user-a"},
            }
        ),
        encoding="utf-8",
    )
    store = JsonStore(path)
    await store.initialize()
    assert store.has_processed_message("message-1")
    assert store.p2p_chat_ids() == {"user-a": "p2p-chat"}
    assert store.get_record("group", "user-a", "2026-07-22") is None
