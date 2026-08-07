import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from song_agent.application.calendar_service import CalendarApplicationService
from song_agent.application.pending_action_service import PendingActionApplicationService
from song_agent.domain.intents import UserRequest
from song_agent.feishu.openapi import FeishuApiError
from song_agent.models import FeishuIdentity
from song_agent.services.audit import AuditService
from song_agent.services.encryption import AesGcmTokenCipher
from song_agent.services.pending_actions import PendingActionService
from song_agent.store import SqliteStore


class OAuth:
    def __init__(self, authorized: bool = True) -> None:
        self.authorized = authorized

    async def get_valid_token_context(self, identity, scopes):
        return SimpleNamespace(subject_id=identity.subject_id) if self.authorized else None

    async def create_authorization_url(self, *args, **kwargs):
        return "https://auth.example"


class DirectOpenApi:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def create_calendar_command(self, command, token, *, idempotency_key):
        self.calls.append((command, token, idempotency_key))
        return {
            "event_id": f"event-{len(self.calls)}",
            "calendar_id": "cal-1",
            "url": "https://feishu.cn/calendar/event-1",
            "request_id": "req-1",
        }


class FailingOpenApi:
    async def create_calendar_command(self, command, token, *, idempotency_key):
        del command, token, idempotency_key
        raise FeishuApiError("calendar unavailable", code=500, retryable=True)


def identity(open_id: str = "open-a") -> FeishuIdentity:
    return FeishuIdentity(
        tenant_key="tenant",
        app_id="app",
        open_id=open_id,
        union_id="subject-a" if open_id == "open-a" else "subject-b",
    )


def request(open_id: str = "open-a") -> UserRequest:
    return UserRequest(
        identity=identity(open_id),
        text="明天提醒我喝水",
        source="react",
        chat_id="chat",
        message_id="message-1",
    )


async def make_store(tmp_path: Path) -> SqliteStore:
    store = SqliteStore(
        tmp_path / "state.db",
        app_id="app",
        token_cipher=AesGcmTokenCipher({1: b"1" * 32}, 1),
    )
    await store.initialize()
    return store


def make_service(
    store: SqliteStore,
    *,
    oauth: OAuth | None = None,
    openapi: object | None = None,
) -> CalendarApplicationService:
    return CalendarApplicationService(
        oauth or OAuth(),
        PendingActionService(store),
        openapi or object(),
        default_timezone="Asia/Shanghai",
        audit=AuditService(store),
    )


@pytest.mark.asyncio
async def test_calendar_prepare_defaults_to_60_minutes_and_never_calls_openapi(
    tmp_path: Path,
) -> None:
    store = await make_store(tmp_path)
    try:
        service = make_service(store)
        start = datetime.now(UTC) + timedelta(days=1)
        result = await service.prepare_create_confirmation(
            request(),
            {"summary": "喝水", "start_time": start.isoformat()},
        )
        action = await store.get_pending_action(result.action_id)
        assert result.status == "awaiting_confirmation"
        assert action and action.status == "awaiting_confirmation"
        parsed_start = datetime.fromisoformat(action.payload["start_time"])
        parsed_end = datetime.fromisoformat(action.payload["end_time"])
        assert parsed_end - parsed_start == timedelta(minutes=60)
        assert (
            await store.ready_outbox_action_ids()
        ) == [], "准备阶段不能直接写入执行 outbox"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_calendar_prepare_rejects_past_and_invalid_end(tmp_path: Path) -> None:
    store = await make_store(tmp_path)
    try:
        service = make_service(store)
        past = datetime.now(UTC) - timedelta(hours=1)
        rejected = await service.prepare_create_confirmation(
            request(),
            {"summary": "过去", "start_time": past.isoformat()},
        )
        start = datetime.now(UTC) + timedelta(days=1)
        invalid = await service.prepare_create_confirmation(
            request(),
            {
                "summary": "倒序",
                "start_time": start.isoformat(),
                "end_time": (start - timedelta(minutes=1)).isoformat(),
            },
        )
        assert rejected.status == invalid.status == "clarification_required"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_calendar_prepare_returns_authorization_before_pending_action(
    tmp_path: Path,
) -> None:
    store = await make_store(tmp_path)
    try:
        service = make_service(store, oauth=OAuth(authorized=False))
        result = await service.prepare_create_confirmation(
            request(),
            {
                "summary": "喝水",
                "start_time": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            },
        )
        assert result.status == "authorization_required"
        assert result.authorization_url == "https://auth.example"
        assert await store.list_pending_actions(
            tenant_key="tenant",
            app_id="app",
            principal_id="subject-a",
        ) == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_pending_action_owner_concurrency_dedupe_and_retry(tmp_path: Path) -> None:
    store = await make_store(tmp_path)
    try:
        calendar = make_service(store)
        prepared = await calendar.prepare_create_confirmation(
            request(),
            {
                "summary": "喝水",
                "start_time": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            },
        )
        service = PendingActionApplicationService(store, AuditService(store))
        denied = await service.confirm(identity("open-b"), prepared.action_id)
        assert denied.status == "error"

        results = await asyncio.gather(
            *(service.confirm(identity(), prepared.action_id) for _ in range(10))
        )
        assert all(result.status == "ok" for result in results)
        row = await (
            await store.db.execute(
                "SELECT COUNT(*) count FROM action_outbox WHERE action_id = ?",
                (prepared.action_id,),
            )
        ).fetchone()
        assert row["count"] == 1

        await store.db.execute(
            "UPDATE pending_actions SET status='failed_retryable' WHERE action_id=?",
            (prepared.action_id,),
        )
        await store.db.commit()
        retried = await service.retry(identity(), prepared.action_id)
        assert retried.status == "ok"
        row = await (
            await store.db.execute(
                "SELECT COUNT(*) count FROM action_outbox WHERE action_id = ?",
                (prepared.action_id,),
            )
        ).fetchone()
        assert row["count"] == 2
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_pending_action_rejects_expired_confirm_and_succeeded_retry(
    tmp_path: Path,
) -> None:
    store = await make_store(tmp_path)
    try:
        calendar = make_service(store)
        prepared = await calendar.prepare_create_confirmation(
            request(),
            {
                "summary": "喝水",
                "start_time": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            },
        )
        service = PendingActionApplicationService(store, AuditService(store))
        await store.db.execute(
            "UPDATE pending_actions SET expires_at=? WHERE action_id=?",
            (int(datetime.now(UTC).timestamp()) - 1, prepared.action_id),
        )
        await store.db.commit()
        expired = await service.confirm(identity(), prepared.action_id)
        assert expired.status == "error"
        assert "过期" in expired.message

        await store.db.execute(
            "UPDATE pending_actions SET status='succeeded' WHERE action_id=?",
            (prepared.action_id,),
        )
        await store.db.commit()
        retry = await service.retry(identity(), prepared.action_id)
        assert retry.status == "error"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_pending_action_card_event_id_is_deduplicated(tmp_path: Path) -> None:
    store = await make_store(tmp_path)
    try:
        calendar = make_service(store)
        prepared = await calendar.prepare_create_confirmation(
            request(),
            {
                "summary": "喝水",
                "start_time": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            },
        )
        service = PendingActionApplicationService(store, AuditService(store))
        first = await service.confirm(identity(), prepared.action_id, event_id="event-1")
        second = await service.confirm(identity(), prepared.action_id, event_id="event-1")
        assert first.status == second.status == "ok"
        row = await (
            await store.db.execute(
                "SELECT COUNT(*) count FROM action_outbox WHERE action_id=?",
                (prepared.action_id,),
            )
        ).fetchone()
        assert row["count"] == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_calendar_create_direct_executes_without_pending_action(
    tmp_path: Path,
) -> None:
    store = await make_store(tmp_path)
    openapi = DirectOpenApi()
    try:
        service = make_service(store, openapi=openapi)
        start = datetime.now(UTC) + timedelta(days=1)
        result = await service.create(
            request(),
            {"summary": "项目会议", "start_time": start.isoformat()},
        )
        assert result.status == "ok"
        assert "已创建日程" in result.message
        assert "项目会议" in result.message
        assert "时间：" in result.message
        assert len(openapi.calls) == 1
        command, token, idempotency_key = openapi.calls[0]
        assert command.summary == "项目会议"
        assert idempotency_key
        assert await store.list_pending_actions(
            tenant_key="tenant",
            app_id="app",
            principal_id="subject-a",
        ) == []
        assert await store.ready_outbox_action_ids() == []
        cursor = await store.db.execute(
            "SELECT operation, result, metadata_json FROM audit_logs"
            " WHERE operation = 'calendar.create'"
        )
        rows = await cursor.fetchall()
        assert len(rows) == 1
        assert rows[0]["result"] == "success"
        metadata = json.loads(rows[0]["metadata_json"])
        assert metadata["resource_id"] == "event-1"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_calendar_create_repeated_delivery_uses_same_idempotency_key(
    tmp_path: Path,
) -> None:
    store = await make_store(tmp_path)
    openapi = DirectOpenApi()
    try:
        service = make_service(store, openapi=openapi)
        arguments = {
            "summary": "喝水",
            "start_time": (
                datetime.now(UTC) + timedelta(days=1)
            ).isoformat(),
        }
        first = await service.create(request(), arguments)
        second = await service.create(request(), arguments)
        assert first.status == second.status == "ok"
        assert len(openapi.calls) == 2
        assert openapi.calls[0][2] == openapi.calls[1][2], (
            "同一 message_id 重复投递必须使用相同幂等键"
        )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_calendar_create_requires_authorization(tmp_path: Path) -> None:
    store = await make_store(tmp_path)
    openapi = DirectOpenApi()
    try:
        service = make_service(store, oauth=OAuth(authorized=False), openapi=openapi)
        result = await service.create(
            request(),
            {
                "summary": "喝水",
                "start_time": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            },
        )
        assert result.status == "authorization_required"
        assert result.authorization_url == "https://auth.example"
        assert openapi.calls == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_calendar_create_with_attendees_goes_confirmation(
    tmp_path: Path,
) -> None:
    store = await make_store(tmp_path)
    openapi = DirectOpenApi()
    try:
        service = make_service(store, openapi=openapi)
        result = await service.create(
            request(),
            {
                "summary": "会议",
                "start_time": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
                "attendee_open_ids": ["open-x"],
            },
        )
        assert result.status == "awaiting_confirmation"
        assert openapi.calls == []
        action = await store.get_pending_action(result.action_id)
        assert action and action.action_type == "calendar.create"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_calendar_create_rejects_invalid_arguments(tmp_path: Path) -> None:
    store = await make_store(tmp_path)
    openapi = DirectOpenApi()
    try:
        service = make_service(store, openapi=openapi)
        past = datetime.now(UTC) - timedelta(hours=1)
        rejected = await service.create(
            request(),
            {"summary": "过去", "start_time": past.isoformat()},
        )
        start = datetime.now(UTC) + timedelta(days=1)
        invalid = await service.create(
            request(),
            {
                "summary": "倒序",
                "start_time": start.isoformat(),
                "end_time": (start - timedelta(minutes=1)).isoformat(),
            },
        )
        assert rejected.status == invalid.status == "clarification_required"
        assert openapi.calls == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_calendar_create_error_returns_message(tmp_path: Path) -> None:
    store = await make_store(tmp_path)
    try:
        service = make_service(store, openapi=FailingOpenApi())
        result = await service.create(
            request(),
            {
                "summary": "喝水",
                "start_time": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            },
        )
        assert result.status == "error"
        assert "创建失败" in result.message
        cursor = await store.db.execute(
            "SELECT result, metadata_json FROM audit_logs"
            " WHERE operation = 'calendar.create'"
        )
        rows = await cursor.fetchall()
        assert rows and rows[0]["result"] == "failed_retryable"
        metadata = json.loads(rows[0]["metadata_json"])
        assert metadata["error_code"] == "500"
    finally:
        await store.close()
