from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from song_agent.application.calendar_service import CalendarApplicationService
from song_agent.application.reminder_service import REMINDER_MARKER, ReminderApplicationService
from song_agent.application.task_service import TaskApplicationService
from song_agent.domain.intents import UserRequest
from song_agent.models import FeishuIdentity
from song_agent.services.encryption import AesGcmTokenCipher
from song_agent.services.pending_actions import PendingActionService
from song_agent.store import SqliteStore


class OAuth:
    async def get_valid_token_context(self, identity, scopes):
        return SimpleNamespace(subject_id=identity.subject_id, access_token="token")

    async def create_authorization_url(self, *args, **kwargs):
        raise AssertionError("authorized test must not request OAuth")


class OpenApi:
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


@pytest.mark.asyncio
async def test_reminder_uses_marked_calendar_actions(tmp_path: Path) -> None:
    store = await make_store(tmp_path)
    try:
        calendar = CalendarApplicationService(
            OAuth(),
            PendingActionService(store),
            OpenApi(),
            default_timezone="Asia/Shanghai",
        )
        service = ReminderApplicationService(calendar)
        start = datetime.now(UTC) + timedelta(hours=1)
        created = await service.prepare_create(
            request(),
            {"summary": "喝水", "start_time": start.isoformat()},
        )
        action = await store.get_pending_action(created.action_id)
        queried = await service.query(request(), {})
        cancelled = await service.prepare_cancel(
            request().model_copy(update={"message_id": "cancel"}),
            {"event_id": "event-1"},
        )

        assert action and action.action_type == "reminder.create"
        assert REMINDER_MARKER in action.payload["description"]
        assert action.payload["reminder_minutes"] == [0]
        assert [item["event_id"] for item in queried.data["items"]] == ["event-1"]
        assert cancelled.intent == "reminder.cancel"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_reminder_accepts_single_integer_reminder_minutes(tmp_path: Path) -> None:
    store = await make_store(tmp_path)
    try:
        calendar = CalendarApplicationService(
            OAuth(),
            PendingActionService(store),
            OpenApi(),
            default_timezone="Asia/Shanghai",
        )
        service = ReminderApplicationService(calendar)
        result = await service.prepare_create(
            request(),
            {
                "summary": "喝水",
                "start_time": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                "reminder_minutes": 0,
            },
        )
        action = await store.get_pending_action(result.action_id)

        assert result.status == "awaiting_confirmation"
        assert action and action.payload["reminder_minutes"] == [0]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_reminder_repairs_equal_start_and_end_to_one_minute(tmp_path: Path) -> None:
    store = await make_store(tmp_path)
    try:
        calendar = CalendarApplicationService(
            OAuth(),
            PendingActionService(store),
            OpenApi(),
            default_timezone="Asia/Shanghai",
        )
        service = ReminderApplicationService(calendar)
        start = datetime.now(UTC) + timedelta(minutes=10)
        result = await service.prepare_create(
            request(),
            {
                "summary": "喝水",
                "start_time": start.isoformat(),
                "end_time": start.isoformat(),
                "reminder_minutes": 0,
            },
        )
        action = await store.get_pending_action(result.action_id)

        assert result.status == "awaiting_confirmation"
        assert action
        normalized_start = datetime.fromisoformat(action.payload["start_time"])
        normalized_end = datetime.fromisoformat(action.payload["end_time"])
        assert normalized_end - normalized_start == timedelta(minutes=1)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_batch_reminders_prepare_independent_actions(tmp_path: Path) -> None:
    store = await make_store(tmp_path)
    try:
        calendar = CalendarApplicationService(
            OAuth(),
            PendingActionService(store),
            OpenApi(),
            default_timezone="Asia/Shanghai",
        )
        service = ReminderApplicationService(calendar)
        tomorrow = datetime.now(UTC) + timedelta(days=1)
        result = await service.prepare_batch_create(
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

        assert result.status == "awaiting_confirmation"
        assert len(result.data["action_ids"]) == 2
        actions = [
            await store.get_pending_action(action_id)
            for action_id in result.data["action_ids"]
        ]
        assert [action.payload["summary"] for action in actions if action] == [
            "起床",
            "复盘",
        ]
        assert actions[1] and actions[1].payload["recurrence"] == "FREQ=DAILY"
    finally:
        await store.close()
