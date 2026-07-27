from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from song_agent.domain.results import ExecutionContext
from song_agent.executors.calendar_executor import CalendarCreateExecutor
from song_agent.executors.registry import ExecutorRegistry
from song_agent.feishu.openapi import FeishuApiError
from song_agent.models import IncomingMessage, UserTokenContext
from song_agent.services.encryption import AesGcmTokenCipher
from song_agent.services.pending_actions import PendingActionService
from song_agent.store import SqliteStore


class OAuth:
    def __init__(self) -> None:
        self.identities = []

    async def get_valid_token_context(self, identity, scopes):
        self.identities.append(identity)
        return UserTokenContext(
            tenant_key=identity.tenant_key,
            app_id=identity.app_id,
            subject_id=identity.subject_id,
            open_id=identity.open_id,
            access_token="user-access-token",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scopes=frozenset(scopes),
        )


class OpenApi:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.tokens = []

    async def create_calendar_command(self, command, token, *, idempotency_key):
        self.tokens.append(token.access_token)
        if self.error:
            raise self.error
        return {
            "event_id": "event-1",
            "calendar_id": "primary",
            "url": "https://example/event-1",
            "request_id": "request-1",
        }


class Audit:
    async def record(self, *args, **kwargs):
        return None


class Transport:
    def __init__(self) -> None:
        self.messages = []

    async def send_markdown(self, *args, **kwargs):
        self.messages.append((args, kwargs))
        return "message"


@pytest.mark.asyncio
async def test_registry_routes_plan_calendar_payload_to_legacy_handler() -> None:
    legacy_actions = []
    direct_actions = []

    async def legacy_handler(action):
        legacy_actions.append(action)

    class DirectExecutor:
        action_type = "calendar.create"

        async def execute(self, action, context):
            direct_actions.append((action, context))

    registry = ExecutorRegistry(legacy_handler=legacy_handler)
    registry.register(DirectExecutor())
    plan_action = SimpleNamespace(
        action_type="calendar.create",
        payload={"record_key": "plan-record"},
    )

    await registry.execute(plan_action)

    assert legacy_actions == [plan_action]
    assert direct_actions == []


@pytest.mark.asyncio
async def test_registry_keeps_direct_calendar_payload_on_executor() -> None:
    legacy_actions = []
    direct_actions = []

    async def legacy_handler(action):
        legacy_actions.append(action)

    class DirectExecutor:
        action_type = "calendar.create"

        async def execute(self, action, context):
            direct_actions.append((action, context))

    registry = ExecutorRegistry(legacy_handler=legacy_handler)
    registry.register(DirectExecutor())
    direct_action = SimpleNamespace(
        action_type="calendar.create",
        payload={"summary": "开会"},
    )

    await registry.execute(direct_action)

    assert legacy_actions == []
    assert direct_actions[0][0] is direct_action


async def setup_action(tmp_path: Path):
    store = SqliteStore(
        tmp_path / "state.db",
        app_id="app",
        token_cipher=AesGcmTokenCipher({1: b"1" * 32}, 1),
    )
    await store.initialize()
    message = IncomingMessage(
        message_id="message",
        tenant_key="tenant",
        app_id="app",
        user_id="subject",
        open_id="open",
        chat_id="chat",
        chat_type="p2p",
        message_type="text",
        text="create",
    )
    start = datetime.now(UTC) + timedelta(days=1)
    payload = {
        "summary": "喝水",
        "start_time": start.isoformat(),
        "end_time": (start + timedelta(hours=1)).isoformat(),
        "timezone": "Asia/Shanghai",
        "reminder_minutes": [10],
        "attendee_open_ids": [],
        "is_all_day": False,
    }
    action = await PendingActionService(store).create_action(
        message,
        action_type="calendar.create",
        payload=payload,
        idempotency_key="key",
        source="react",
    )
    assert await store.claim_pending_action(
        action.action_id,
        actor_open_id="open",
        payload_hash=action.payload_hash,
    )
    return store, action


@pytest.mark.asyncio
async def test_calendar_executor_uses_user_token_and_saves_result(tmp_path: Path) -> None:
    store, action = await setup_action(tmp_path)
    try:
        oauth = OAuth()
        openapi = OpenApi()
        transport = Transport()
        executor = CalendarCreateExecutor(
            store,
            oauth,
            openapi,
            Audit(),
            transport,
        )
        result = await executor.execute(action, ExecutionContext(worker_id="test"))
        stored = await store.get_pending_action(action.action_id)
        assert result.status == "succeeded"
        assert openapi.tokens == ["user-access-token"]
        assert stored and stored.status == "succeeded"
        assert stored.result["event_id"] == "event-1"
        assert transport.messages[0][0] == (
            "chat",
            "✅ 已创建日程：[喝水](https://example/event-1)",
        )
        assert stored.remote_resource_id == "event-1"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_calendar_executor_maps_429_to_retryable(tmp_path: Path) -> None:
    store, action = await setup_action(tmp_path)
    try:
        executor = CalendarCreateExecutor(
            store,
            OAuth(),
            OpenApi(
                FeishuApiError(
                    "rate limited",
                    code=429,
                    retryable=True,
                    request_id="request-429",
                )
            ),
            Audit(),
            Transport(),
        )
        result = await executor.execute(action, ExecutionContext(worker_id="test"))
        stored = await store.get_pending_action(action.action_id)
        assert result.status == "failed_retryable"
        assert stored and stored.status == "failed_retryable"
        assert stored.error_code == "429"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_calendar_executor_maps_parameter_4xx_to_final_failure(
    tmp_path: Path,
) -> None:
    store, action = await setup_action(tmp_path)
    try:
        executor = CalendarCreateExecutor(
            store,
            OAuth(),
            OpenApi(
                FeishuApiError(
                    "invalid event",
                    code=400,
                    retryable=False,
                    request_id="request-400",
                )
            ),
            Audit(),
            Transport(),
        )
        result = await executor.execute(action, ExecutionContext(worker_id="test"))
        stored = await store.get_pending_action(action.action_id)
        assert result.status == "failed_final"
        assert stored and stored.status == "failed_final"
        assert stored.error_code == "400"
    finally:
        await store.close()
