import asyncio
import json
import time
from pathlib import Path

import pytest

from song_agent.channels.feishu.cards import calendar_confirmation_card, document_confirmation_card
from song_agent.models import DailyRecord, IncomingMessage, PlanTask
from song_agent.services.encryption import AesGcmTokenCipher
from song_agent.services.pending_actions import PendingActionService
from song_agent.store import SqliteStore


def message() -> IncomingMessage:
    return IncomingMessage(
        message_id="message-1",
        tenant_key="tenant",
        app_id="app",
        user_id="union-a",
        open_id="open-a",
        union_id="union-a",
        chat_id="chat",
        thread_id="thread",
        chat_type="group",
        message_type="text",
        text="十点提醒我开会",
    )


def record(store: SqliteStore) -> DailyRecord:
    return DailyRecord(
        key=store.record_key(
            "chat",
            "union-a",
            "2026-07-24",
            tenant_key="tenant",
            thread_id="thread",
        ),
        date="2026-07-24",
        tenant_key="tenant",
        app_id="app",
        chat_id="chat",
        thread_id="thread",
        user_id="union-a",
        plan_status="draft",
        tasks=[PlanTask(id="A1", priority="A", title="开会", start_time="10:00")],
        created_at="2026-07-24T00:00:00+00:00",
        updated_at="2026-07-24T00:00:00+00:00",
    )


@pytest.mark.asyncio
async def test_pending_action_binds_creator_hash_expiry_and_exactly_once(tmp_path: Path) -> None:
    store = SqliteStore(
        tmp_path / "state.db",
        app_id="app",
        token_cipher=AesGcmTokenCipher({1: b"1" * 32}, 1),
    )
    await store.initialize()
    try:
        service = PendingActionService(store)
        action = await service.create_action(
            message(),
            action_type="calendar.create",
            payload={"summary": "开会", "start_time": "2026-07-24T10:00:00+08:00"},
            idempotency_key="bind-test",
            source="test",
        )
        card = calendar_confirmation_card("待确认", action)
        serialized = json.dumps(card)
        assert action.action_id in serialized
        assert action.payload_hash not in serialized
        assert "pending_action.confirm" in serialized
        assert "access_token" not in serialized and "refresh_token" not in serialized
        assert not await store.claim_pending_action(
            action.action_id,
            actor_open_id="open-b",
            payload_hash=action.payload_hash,
        )
        assert not await store.claim_pending_action(
            action.action_id,
            actor_open_id="open-a",
            payload_hash="tampered",
        )
        results = await asyncio.gather(
            *(
                store.claim_pending_action(
                    action.action_id,
                    actor_open_id="open-a",
                    payload_hash=action.payload_hash,
                )
                for _ in range(10)
            )
        )
        assert results.count(True) == 1
        assert results.count(False) == 9
        assert await store.claim_action_execution(
            action.action_id,
            worker_id="test-worker",
        )
        assert await store.finish_pending_action(action.action_id, success=True)
        stored = await store.get_pending_action(action.action_id)
        assert stored and stored.status == "succeeded"
        attempt = await (
            await store.db.execute(
                "SELECT status FROM action_attempts WHERE action_id = ?",
                (action.action_id,),
            )
        ).fetchone()
        outbox = await (
            await store.db.execute(
                "SELECT status FROM action_outbox WHERE action_id = ?",
                (action.action_id,),
            )
        ).fetchone()
        assert attempt["status"] == "succeeded"
        assert outbox["status"] == "processed"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_expired_pending_action_cannot_execute(tmp_path: Path) -> None:
    store = SqliteStore(
        tmp_path / "state.db",
        app_id="app",
        token_cipher=AesGcmTokenCipher({1: b"1" * 32}, 1),
    )
    await store.initialize()
    try:
        service = PendingActionService(store, ttl_seconds=-1)
        action = await service.create_action(
            message(),
            action_type="calendar.create",
            payload={"summary": "开会", "start_time": "2026-07-24T10:00:00+08:00"},
            idempotency_key="bind-test",
            source="test",
        )
        assert action.expires_at < int(time.time())
        assert not await store.claim_pending_action(
            action.action_id,
            actor_open_id="open-a",
            payload_hash=action.payload_hash,
        )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_document_card_contains_only_action_locator_not_document_content(
    tmp_path: Path,
) -> None:
    store = SqliteStore(
        tmp_path / "state.db",
        app_id="app",
        token_cipher=AesGcmTokenCipher({1: b"1" * 32}, 1),
    )
    await store.initialize()
    try:
        service = PendingActionService(store)
        action = await service.create_document_action(
            message(),
            action_type="document.append",
            title="项目方案",
            markdown="confidential body",
            document_token="doc-token",
            document_url="https://feishu.cn/docx/doc-token",
        )
        serialized = json.dumps(document_confirmation_card("安全摘要", action))
        assert action.action_id in serialized
        assert action.payload_hash not in serialized
        assert "pending_action.confirm" in serialized
        assert "confidential body" not in serialized
        assert "doc-token" not in serialized
        stored = await store.get_pending_action(action.action_id)
        assert stored is not None
        assert stored.payload["markdown"] == "confidential body"
    finally:
        await store.close()
