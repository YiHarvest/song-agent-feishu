from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from song_agent.application.calendar_service import CalendarApplicationService
from song_agent.application.reminder_service import REMINDER_MARKER, ReminderApplicationService
from song_agent.application.task_service import TaskApplicationService
from song_agent.domain.intents import UserRequest
from song_agent.models import FeishuIdentity
from song_agent.services.audit import AuditService
from song_agent.services.encryption import AesGcmTokenCipher
from song_agent.services.pending_actions import PendingActionService
from song_agent.store import SqliteStore


class OAuth:
    async def get_valid_token_context(self, identity, scopes):
        return SimpleNamespace(subject_id=identity.subject_id, access_token="token")

    async def create_authorization_url(self, *args, **kwargs):
        raise AssertionError("authorized test must not request OAuth")


class OpenApi:
    def __init__(self) -> None:
        self.created: list[tuple] = []

    async def create_calendar_command(self, command, token, *, idempotency_key):
        self.created.append((command, token, idempotency_key))
        return {
            "event_id": f"event-{len(self.created)}",
            "calendar_id": "cal-1",
            "url": "",
            "request_id": "req-1",
        }

    async def query_tasks(self, command, token):
        return {"items": [{"guid": "task-1", "summary": "测试"}]}

    async def query_calendar(self, command, token):
        return {
            "items": [
                {"event_id": "event-1", "description": REMINDER_MARKER},
                {"event_id": "event-2", "description": "普通日程"},
            ]
        }


async def make_store(tmp_path: Path) -> SqliteStore:
    store = SqliteStore(
        tmp_path / "business.db",
        app_id="app",
        token_cipher=AesGcmTokenCipher({1: b"1" * 32}, 1),
    )
    await store.initialize()
    return store


def request() -> UserRequest:
    return UserRequest(
        identity=FeishuIdentity(
            tenant_key="tenant",
            app_id="app",
            open_id="open-a",
            union_id="union-a",
        ),
        text="创建任务",
        source="api",
        chat_id="chat",
        message_id="message",
    )


@pytest.mark.asyncio
async def test_task_crud_prepares_pending_actions_and_queries_feishu(
    tmp_path: Path,
) -> None:
    store = await make_store(tmp_path)
    try:
        service = TaskApplicationService(
            OAuth(),
            PendingActionService(store),
            OpenApi(),
        )
        due = datetime.now(UTC) + timedelta(hours=1)
        created = await service.prepare_create(
            request(),
            {"summary": "测试", "due_time": due.isoformat()},
        )
        action = await store.get_pending_action(created.action_id)
        queried = await service.query(request(), {})
        updated = await service.prepare_update(
            request().model_copy(update={"message_id": "update"}),
            {"task_guid": "task-1", "fields": {"summary": "新标题"}},
        )
        completed = await service.prepare_complete(
            request().model_copy(update={"message_id": "complete"}),
            {"task_guid": "task-1"},
        )
        deleted = await service.prepare_delete(
            request().model_copy(update={"message_id": "delete"}),
            {"task_guid": "task-1"},
        )

        assert action
        assert action.payload["assignee_open_ids"] == ["open-a"]
        assert queried.data["items"][0]["guid"] == "task-1"
        assert {updated.intent, completed.intent, deleted.intent} == {
            "task.update",
            "task.complete",
            "task.delete",
        }
    finally:
        await store.close()


def make_reminder_service(
    store: SqliteStore,
    openapi: OpenApi,
) -> ReminderApplicationService:
    calendar = CalendarApplicationService(
        OAuth(),
        PendingActionService(store),
        openapi,
        default_timezone="Asia/Shanghai",
        audit=AuditService(store),
    )
    return ReminderApplicationService(calendar)


@pytest.mark.asyncio
async def test_reminder_create_direct_marks_and_queries(tmp_path: Path) -> None:
    store = await make_store(tmp_path)
    openapi = OpenApi()
    try:
        service = make_reminder_service(store, openapi)
        start = datetime.now(UTC) + timedelta(hours=1)
        created = await service.create(
            request(),
            {"summary": "喝水", "start_time": start.isoformat()},
        )
        queried = await service.query(request(), {})
        cancelled = await service.prepare_cancel(
            request().model_copy(update={"message_id": "cancel"}),
            {"event_id": "event-1"},
        )

        assert created.status == "ok"
        assert "已创建提醒" in created.message
        assert len(openapi.created) == 1
        command = openapi.created[0][0]
        assert REMINDER_MARKER in command.description
        assert command.reminder_minutes == [0]
        assert [item["event_id"] for item in queried.data["items"]] == ["event-1"]
        assert cancelled.intent == "reminder.cancel"
        remaining = await store.list_pending_actions(
            tenant_key="tenant",
            app_id="app",
            principal_id="union-a",
        )
        assert all(
            action.action_type != "reminder.create"
            for action in remaining
        ), "普通提醒创建不得产生 PendingAction"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_reminder_accepts_single_integer_reminder_minutes(tmp_path: Path) -> None:
    store = await make_store(tmp_path)
    openapi = OpenApi()
    try:
        service = make_reminder_service(store, openapi)
        result = await service.create(
            request(),
            {
                "summary": "喝水",
                "start_time": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                "reminder_minutes": 0,
            },
        )
        assert result.status == "ok"
        command = openapi.created[0][0]
        assert command.reminder_minutes == [0]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_reminder_repairs_equal_start_and_end_to_one_minute(
    tmp_path: Path,
) -> None:
    store = await make_store(tmp_path)
    openapi = OpenApi()
    try:
        service = make_reminder_service(store, openapi)
        start = datetime.now(UTC) + timedelta(minutes=10)
        result = await service.create(
            request(),
            {
                "summary": "喝水",
                "start_time": start.isoformat(),
                "end_time": start.isoformat(),
                "reminder_minutes": 0,
            },
        )
        assert result.status == "ok"
        command = openapi.created[0][0]
        assert command.end_time - command.start_time == timedelta(minutes=1)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_batch_reminders_direct_create_all(tmp_path: Path) -> None:
    store = await make_store(tmp_path)
    openapi = OpenApi()
    try:
        service = make_reminder_service(store, openapi)
        tomorrow = datetime.now(UTC) + timedelta(days=1)
        result = await service.create_batch(
            request(),
            {
                "items": [
                    {"summary": "起床", "start_time": tomorrow.isoformat()},
                    {
                        "summary": "复盘",
                        "start_time": (tomorrow + timedelta(hours=1)).isoformat(),
                        "recurrence": "FREQ=DAILY",
                    },
                ]
            },
        )

        assert result.status == "ok"
        assert "2" in result.message
        assert len(openapi.created) == 2
        commands = [call[0] for call in openapi.created]
        assert [command.summary for command in commands] == ["起床", "复盘"]
        assert commands[1].recurrence == "FREQ=DAILY"
        assert await store.list_pending_actions(
            tenant_key="tenant",
            app_id="app",
            principal_id="union-a",
        ) == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_batch_reminders_with_attendee_goes_confirmation(tmp_path: Path) -> None:
    store = await make_store(tmp_path)
    openapi = OpenApi()
    try:
        service = make_reminder_service(store, openapi)
        tomorrow = datetime.now(UTC) + timedelta(days=1)
        result = await service.create_batch(
            request(),
            {
                "items": [
                    {"summary": "起床", "start_time": tomorrow.isoformat()},
                    {
                        "summary": "复盘",
                        "start_time": (tomorrow + timedelta(hours=1)).isoformat(),
                        "attendee_open_ids": ["open-x"],
                    },
                ]
            },
        )

        assert result.status == "awaiting_confirmation"
        assert openapi.created == []
        assert len(result.data["action_ids"]) == 2
        actions = [
            await store.get_pending_action(action_id)
            for action_id in result.data["action_ids"]
        ]
        assert [action.action_type for action in actions if action] == [
            "reminder.create",
            "reminder.create",
        ]
    finally:
        await store.close()
