from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI

from song_agent.api.agent_auth import ApiCredential
from song_agent.api.agent_schemas import ChatCompletionRequest
from song_agent.app import create_app
from song_agent.application.calendar_service import CalendarApplicationService
from song_agent.application.openai_adapter import OpenAIAdapter
from song_agent.application.reminder_service import ReminderApplicationService
from song_agent.config import Settings
from song_agent.context.builders import BusinessContextBuilder
from song_agent.domain.intents import UserRequest
from song_agent.domain.results import ApplicationResult
from song_agent.models import FeishuIdentity
from song_agent.services.api_access import ApiAccessService, binding_code_hash
from song_agent.services.audit import AuditService
from song_agent.services.encryption import AesGcmTokenCipher
from song_agent.services.pending_actions import PendingActionService
from song_agent.store import SqliteStore


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "feishu_app_id": "feishu-app",
        "feishu_app_secret": "feishu-secret",
        "feishu_encrypt_key": "",
        "feishu_verification_token": "",
        "llm_base_url": "https://llm.example/v1",
        "llm_api_key": "llm-key",
        "llm_model": "llm-model",
        "song_agent_api_enabled": True,
        "song_agent_api_key": "sk-test",
        "song_agent_api_model_id": "song-agent-test",
        "song_agent_api_default_tenant": "api-tenant",
        "song_agent_api_app_id": "api-app",
        "song_agent_api_rate_limit_per_minute": 30,
    }
    values.update(overrides)
    return Settings(**values)


def test_agent_api_settings_load_song_agent_api_environment(monkeypatch) -> None:
    monkeypatch.setenv("SONG_AGENT_API_ENABLED", "true")
    monkeypatch.setenv("SONG_AGENT_API_MODEL_ID", "env-model")
    monkeypatch.setenv("SONG_AGENT_API_KEY", "env-secret")
    configured = Settings(
        _env_file=None,
        feishu_app_id="feishu-app",
        feishu_app_secret="feishu-secret",
        llm_base_url="https://llm.example/v1",
        llm_api_key="llm-key",
        llm_model="llm-model",
    )

    assert configured.song_agent_api_enabled is True
    assert configured.song_agent_api_model_id == "env-model"
    assert configured.song_agent_api_key
    assert configured.song_agent_api_key.get_secret_value() == "env-secret"
    assert "env-secret" not in repr(configured)


async def api_request(app, method: str, path: str, **kwargs):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        return await client.request(method, path, **kwargs)


async def make_store(tmp_path: Path) -> SqliteStore:
    store = SqliteStore(
        tmp_path / "agent-api.db",
        app_id="feishu-app",
        token_cipher=AesGcmTokenCipher({1: b"x" * 32}, 1),
    )
    await store.initialize()
    return store


class FakeAdapter:
    async def complete(self, payload, credential, **kwargs):
        del credential, kwargs
        return (
            {
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 1,
                "model": payload.model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "你好"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
            False,
        )


class FakeRouter:
    def __init__(self) -> None:
        self.requests = []

    async def dispatch(self, request):
        self.requests.append(request)
        return ApplicationResult(
            status="ok",
            intent="conversation.general",
            message="adapter reply",
        )


@pytest.mark.asyncio
async def test_api_router_can_be_disabled_without_removing_feishu_routes() -> None:
    app = create_app(settings(song_agent_api_enabled=False))

    api = await api_request(app, "GET", "/api/v1/models")
    feishu = await api_request(app, "POST", "/feishu/card/action", content=b"{}")

    assert api.status_code == 404
    assert feishu.status_code == 503


@pytest.mark.asyncio
async def test_api_configuration_error_returns_503_without_breaking_app() -> None:
    app = create_app(settings(song_agent_api_key=None))

    response = await api_request(
        app,
        "GET",
        "/api/v1/models",
        headers={"Authorization": "Bearer anything"},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "api_not_configured"


@pytest.mark.asyncio
async def test_api_requires_bearer_key_and_uses_openai_error_envelope() -> None:
    app = create_app(settings())

    response = await api_request(app, "GET", "/api/v1/models")

    assert response.status_code == 401
    assert response.json() == {
        "error": {
            "message": "Invalid API key.",
            "type": "authentication_error",
            "param": None,
            "code": "invalid_api_key",
        }
    }
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.asyncio
async def test_models_list_and_retrieve_use_configured_model() -> None:
    app = create_app(settings())
    headers = {"Authorization": "Bearer sk-test"}

    listed = await api_request(app, "GET", "/api/v1/models", headers=headers)
    retrieved = await api_request(
        app,
        "GET",
        "/api/v1/models/song-agent-test",
        headers=headers,
    )

    assert listed.json()["data"][0]["id"] == "song-agent-test"
    assert retrieved.json()["id"] == "song-agent-test"


@pytest.mark.asyncio
async def test_unknown_model_returns_model_not_found() -> None:
    app = create_app(settings())

    response = await api_request(
        app,
        "GET",
        "/api/v1/models/missing",
        headers={"Authorization": "Bearer sk-test"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"


@pytest.mark.asyncio
async def test_root_v1_alias_is_not_exposed() -> None:
    app = create_app(settings())

    response = await api_request(
        app,
        "GET",
        "/v1/models",
        headers={"Authorization": "Bearer sk-test"},
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_chat_completion_has_openai_shape() -> None:
    app = create_app(settings())
    app.state.openai_adapter = FakeAdapter()

    response = await api_request(
        app,
        "POST",
        "/api/v1/chat/completions",
        headers={"Authorization": "Bearer sk-test"},
        json={
            "model": "song-agent-test",
            "messages": [{"role": "user", "content": "你好"}],
        },
    )

    assert response.status_code == 200
    assert response.json()["object"] == "chat.completion"
    assert response.json()["choices"][0]["message"]["content"] == "你好"


@pytest.mark.asyncio
async def test_streaming_chat_completion_ends_with_done() -> None:
    app = create_app(settings())
    app.state.openai_adapter = FakeAdapter()

    response = await api_request(
        app,
        "POST",
        "/api/v1/chat/completions",
        headers={"Authorization": "Bearer sk-test"},
        json={
            "model": "song-agent-test",
            "stream": True,
            "stream_options": {"include_usage": True},
            "messages": [{"role": "user", "content": "你好"}],
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "chat.completion.chunk" in response.text
    assert response.text.endswith("data: [DONE]\n\n")
    assert '"usage"' in response.text


@pytest.mark.asyncio
async def test_openai_python_sdk_calls_api_v1_base_url() -> None:
    app = create_app(settings())
    app.state.openai_adapter = FakeAdapter()
    http_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    )
    client = AsyncOpenAI(
        base_url="http://test/api/v1",
        api_key="sk-test",
        http_client=http_client,
    )
    try:
        completion = await client.chat.completions.create(
            model="song-agent-test",
            messages=[{"role": "user", "content": "你好"}],
        )
        assert completion.choices[0].message.content == "你好"
    finally:
        await client.close()


def test_all_required_api_v1_routes_are_registered() -> None:
    app = create_app(settings())
    paths = app.openapi()["paths"]
    routes = {
        (method.upper(), path)
        for path, operations in paths.items()
        for method in operations
    }
    assert {
        ("GET", "/api/v1/models"),
        ("GET", "/api/v1/models/{model_id}"),
        ("POST", "/api/v1/chat/completions"),
        ("GET", "/api/v1/capabilities"),
        ("GET", "/api/v1/health"),
        ("GET", "/api/v1/health/details"),
        ("POST", "/api/v1/pending-actions/{action_id}/confirm"),
        ("POST", "/api/v1/pending-actions/{action_id}/cancel"),
        ("POST", "/api/v1/channel-bindings/feishu/code"),
        ("GET", "/api/v1/channel-bindings"),
        ("DELETE", "/api/v1/channel-bindings/{binding_id}"),
    }.issubset(routes)


@pytest.mark.asyncio
async def test_rate_limit_only_applies_to_agent_api() -> None:
    app = create_app(settings(song_agent_api_rate_limit_per_minute=1))
    headers = {"Authorization": "Bearer sk-test"}

    first = await api_request(app, "GET", "/api/v1/models", headers=headers)
    second = await api_request(app, "GET", "/api/v1/models", headers=headers)
    feishu = await api_request(app, "POST", "/feishu/card/action", content=b"{}")

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "rate_limit_exceeded"
    assert feishu.status_code == 503


@pytest.mark.asyncio
async def test_openai_adapter_rejects_tools_with_http_exception(tmp_path: Path) -> None:
    store = await make_store(tmp_path)
    try:
        adapter = OpenAIAdapter(settings(), store, FakeRouter())  # type: ignore[arg-type]
        payload = ChatCompletionRequest.model_validate(
            {
                "model": "song-agent-test",
                "messages": [{"role": "user", "content": "hello"}],
                "tools": [],
            }
        )

        with pytest.raises(Exception) as caught:
            await adapter.complete(
                payload,
                ApiCredential("mentor", "api-tenant", "mentor"),
                conversation_id="conversation",
                header_user_id="user",
                idempotency_key="",
            )

        assert getattr(caught.value, "status_code", None) == 400
        assert caught.value.detail["code"] == "unsupported_parameter"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_openai_adapter_rejects_non_text_content(tmp_path: Path) -> None:
    store = await make_store(tmp_path)
    try:
        adapter = OpenAIAdapter(settings(), store, FakeRouter())  # type: ignore[arg-type]
        payload = ChatCompletionRequest.model_validate(
            {
                "model": "song-agent-test",
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "image_url", "image_url": {"url": "x"}}],
                    }
                ],
            }
        )

        with pytest.raises(Exception) as caught:
            await adapter.complete(
                payload,
                ApiCredential("mentor", "api-tenant", "mentor"),
                conversation_id="conversation",
                header_user_id="user",
                idempotency_key="",
            )

        assert caught.value.detail["code"] == "unsupported_content_type"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_adapter_dispatches_with_api_identity(tmp_path: Path) -> None:
    store = await make_store(tmp_path)
    router = FakeRouter()
    try:
        adapter = OpenAIAdapter(settings(), store, router)  # type: ignore[arg-type]
        payload = ChatCompletionRequest.model_validate(
            {
                "model": "song-agent-test",
                "messages": [{"role": "user", "content": "hello"}],
            }
        )

        response, replayed = await adapter.complete(
            payload,
            ApiCredential("mentor", "api-tenant", "mentor"),
            conversation_id="conversation-1",
            header_user_id="user-1",
            idempotency_key="",
        )

        assert replayed is False
        assert response["choices"][0]["message"]["content"] == "adapter reply"
        assert len(router.requests) == 1
        request = router.requests[0]
        assert request.source == "api"
        assert request.identity.tenant_key == "api-tenant"
        assert request.identity.app_id == "api-app"
        assert request.identity.subject_id == "user-1"
        assert request.chat_id == "conversation-1"
        assert request.delivery_channel == "api"
        assert request.delivery_binding_id is None
        assert request.context["delivery_channel"] == "api"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_idempotent_chat_replays_without_second_router_call(tmp_path: Path) -> None:
    store = await make_store(tmp_path)
    router = FakeRouter()
    try:
        adapter = OpenAIAdapter(settings(), store, router)  # type: ignore[arg-type]
        payload = ChatCompletionRequest.model_validate(
            {
                "model": "song-agent-test",
                "messages": [{"role": "user", "content": "hello"}],
            }
        )
        arguments = {
            "conversation_id": "conversation",
            "header_user_id": "user",
            "idempotency_key": "same-key",
        }

        first, first_replayed = await adapter.complete(
            payload,
            ApiCredential("mentor", "api-tenant", "mentor"),
            **arguments,
        )
        second, second_replayed = await adapter.complete(
            payload,
            ApiCredential("mentor", "api-tenant", "mentor"),
            **arguments,
        )

        assert first == second
        assert first_replayed is False
        assert second_replayed is True
        assert len(router.requests) == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_idempotency_key_conflict_is_409(tmp_path: Path) -> None:
    store = await make_store(tmp_path)
    router = FakeRouter()
    try:
        adapter = OpenAIAdapter(settings(), store, router)  # type: ignore[arg-type]
        credential = ApiCredential("mentor", "api-tenant", "mentor")
        first = ChatCompletionRequest.model_validate(
            {
                "model": "song-agent-test",
                "messages": [{"role": "user", "content": "first"}],
            }
        )
        second = ChatCompletionRequest.model_validate(
            {
                "model": "song-agent-test",
                "messages": [{"role": "user", "content": "second"}],
            }
        )
        await adapter.complete(
            first,
            credential,
            conversation_id="conversation",
            header_user_id="user",
            idempotency_key="same-key",
        )

        with pytest.raises(Exception) as caught:
            await adapter.complete(
                second,
                credential,
                conversation_id="conversation",
                header_user_id="user",
                idempotency_key="same-key",
            )

        assert getattr(caught.value, "status_code", None) == 409
        assert caught.value.detail["code"] == "idempotency_conflict"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_feishu_binding_code_is_one_time_and_binding_is_deletable(tmp_path: Path) -> None:
    store = await make_store(tmp_path)
    service = ApiAccessService(
        store,
        api_app_id="api-app",
        binding_code_ttl_seconds=600,
    )
    identity = FeishuIdentity(
        tenant_key="feishu-tenant",
        app_id="feishu-app",
        open_id="open-user",
        union_id="union-user",
    )
    try:
        code, expires_at = await service.create_binding_code(
            tenant_key="api-tenant",
            principal_id="api-user",
        )
        binding = await service.redeem_binding_code(
            code,
            identity=identity,
            chat_id="feishu-chat",
        )
        replay = await service.redeem_binding_code(
            code,
            identity=identity,
            chat_id="feishu-chat",
        )
        bindings = await store.list_api_channel_bindings(
            tenant_key="api-tenant",
            app_id="api-app",
            principal_id="api-user",
        )

        assert expires_at > 0
        assert binding is not None
        assert replay is None
        assert bindings == [binding]
        assert binding.external_subject_id == "union-user"
        assert await store.delete_api_channel_binding(
            binding.binding_id,
            tenant_key="api-tenant",
            app_id="api-app",
            principal_id="api-user",
        )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_bound_api_request_uses_feishu_identity_without_changing_source(
    tmp_path: Path,
) -> None:
    store = await make_store(tmp_path)
    router = FakeRouter()
    service = ApiAccessService(
        store,
        api_app_id="api-app",
        binding_code_ttl_seconds=600,
    )
    try:
        code, _ = await service.create_binding_code(
            tenant_key="api-tenant",
            principal_id="api-user",
        )
        binding = await service.redeem_binding_code(
            code,
            identity=FeishuIdentity(
                tenant_key="feishu-tenant",
                app_id="feishu-app",
                open_id="open-user",
                union_id="union-user",
            ),
            chat_id="feishu-chat",
        )
        assert binding is not None
        adapter = OpenAIAdapter(settings(), store, router)  # type: ignore[arg-type]
        payload = ChatCompletionRequest.model_validate(
            {
                "model": "song-agent-test",
                "messages": [{"role": "user", "content": "一小时后提醒我"}],
                "metadata": {
                    "delivery_channel": "feishu",
                    "delivery_binding_id": binding.binding_id,
                },
            }
        )

        await adapter.complete(
            payload,
            ApiCredential("mentor", "api-tenant", "mentor"),
            conversation_id="",
            header_user_id="api-user",
            idempotency_key="",
        )

        request = router.requests[0]
        assert request.source == "api"
        assert request.identity.tenant_key == "feishu-tenant"
        assert request.identity.app_id == "feishu-app"
        assert request.identity.subject_id == "union-user"
        assert request.chat_id == "feishu-chat"
        assert request.delivery_channel == "feishu"
        assert request.delivery_binding_id == binding.binding_id
        assert request.context["delivery_channel"] == "feishu"
        assert request.context["delivery_binding_id"] == binding.binding_id
    finally:
        await store.close()


def test_binding_codes_are_hashed_before_persistence() -> None:
    assert binding_code_hash("ABCD1234") != "ABCD1234"
    assert binding_code_hash("abcd1234") == binding_code_hash("ABCD1234")


def test_feishu_request_context_identity_is_unchanged() -> None:
    request = UserRequest(
        identity=FeishuIdentity(
            tenant_key="feishu-tenant",
            app_id="feishu-app",
            open_id="open-user",
            union_id="union-user",
        ),
        text="你好",
        source="feishu",
        chat_id="feishu-chat",
        thread_id="feishu-thread",
        message_id="feishu-message",
    )
    context = BusinessContextBuilder(object(), timezone="Asia/Shanghai").request_context(  # type: ignore[arg-type]
        request
    )

    assert context.channel == "feishu"
    assert context.app_id == "feishu-app"
    assert context.principal_id == "union-user"
    assert context.chat_id == "feishu-chat"


@pytest.mark.asyncio
async def test_feishu_reminder_keeps_original_delivery_target(tmp_path: Path) -> None:
    class OAuth:
        async def get_valid_token_context(self, identity, scopes):
            del scopes
            return SimpleNamespace(subject_id=identity.subject_id)

    class OpenApi:
        async def create_calendar_command(self, command, token, *, idempotency_key):
            del command, token, idempotency_key
            return {
                "event_id": "event-1",
                "calendar_id": "cal-1",
                "url": "",
                "request_id": "req-1",
            }

    store = await make_store(tmp_path)
    try:
        calendar = CalendarApplicationService(
            OAuth(),  # type: ignore[arg-type]
            PendingActionService(store),
            OpenApi(),  # type: ignore[arg-type]
            default_timezone="Asia/Shanghai",
            audit=AuditService(store),
        )
        reminder = ReminderApplicationService(calendar)
        request = UserRequest(
            identity=FeishuIdentity(
                tenant_key="feishu-tenant",
                app_id="feishu-app",
                open_id="open-user",
                union_id="union-user",
            ),
            text="一小时后提醒我",
            source="feishu",
            chat_id="feishu-chat",
            thread_id="feishu-thread",
            message_id="feishu-message",
        )

        result = await reminder.create(
            request,
            {
                "summary": "喝水",
                "start_time": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            },
        )

        assert result.status == "ok"
        assert await store.list_pending_actions(
            tenant_key="feishu-tenant",
            app_id="feishu-app",
            principal_id="union-user",
        ) == []
        cursor = await store.db.execute(
            "SELECT tenant_key, app_id, principal_id, chat_id, thread_id,"
            " message_id, operation FROM audit_logs WHERE operation = 'reminder.create'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row["tenant_key"] == "feishu-tenant"
        assert row["app_id"] == "feishu-app"
        assert row["principal_id"] == "union-user"
        assert row["chat_id"] == "feishu-chat"
        assert row["thread_id"] == "feishu-thread"
        assert row["message_id"] == "feishu-message"
    finally:
        await store.close()
