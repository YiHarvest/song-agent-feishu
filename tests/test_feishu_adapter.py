from __future__ import annotations

import httpx
import pytest

from song_agent.adapters.feishu_workspace import FeishuWorkspaceProvider
from song_agent.kernel.errors import DeliveryState, ProviderError
from song_agent.kernel.models import PrincipalIdentity


def principal(principal_id: str = "user") -> PrincipalIdentity:
    return PrincipalIdentity(
        tenant_id="tenant", principal_id=principal_id, auth_provider="feishu_event"
    )


@pytest.mark.asyncio
async def test_feishu_workspace_provider_scopes_credential_to_principal() -> None:
    provider = FeishuWorkspaceProvider(
        base_url="https://open.feishu.cn",
        user_access_token="secret",
        tenant_id="tenant",
        principal_id="user",
        calendar_id="calendar",
    )
    with pytest.raises(ProviderError) as captured:
        await provider.query(principal("other"), {})
    assert captured.value.error_code == "feishu.principal_not_bound"
    assert captured.value.delivery_state is DeliveryState.NOT_SENT


@pytest.mark.asyncio
async def test_feishu_calendar_create_translates_neutral_arguments_and_idempotency() -> None:
    request_seen: httpx.Request | None = None

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal request_seen
        request_seen = request
        return httpx.Response(
            200,
            json={"code": 0, "data": {"event": {"event_id": "event-1"}}},
            headers={"x-tt-logid": "request-1"},
        )

    provider = FeishuWorkspaceProvider(
        base_url="https://open.feishu.cn",
        user_access_token="secret",
        tenant_id="tenant",
        principal_id="user",
        calendar_id="calendar",
        transport=httpx.MockTransport(respond),
    )
    result = await provider.create(
        principal(),
        {
            "summary": "meeting",
            "start_time": "2026-08-07T10:00:00+08:00",
            "end_time": "2026-08-07T11:00:00+08:00",
            "timezone": "Asia/Shanghai",
            "description": "",
            "location": "",
            "attendee_ids": [],
        },
        idempotency_key="stable-key",
    )
    assert result.remote_resource_id == "event-1"
    assert result.provider_request_id == "request-1"
    assert request_seen is not None
    assert request_seen.url.params["idempotency_key"] == "stable-key"
    assert request_seen.headers["authorization"] == "Bearer secret"


@pytest.mark.asyncio
async def test_feishu_write_read_timeout_is_unknown() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout", request=request)

    provider = FeishuWorkspaceProvider(
        base_url="https://open.feishu.cn",
        user_access_token="secret",
        tenant_id="tenant",
        principal_id="user",
        calendar_id="calendar",
        transport=httpx.MockTransport(timeout),
    )
    with pytest.raises(ProviderError) as captured:
        await provider.delete(
            principal(),
            {"event_id": "event-1", "calendar_id": "calendar"},
            idempotency_key="stable-key",
        )
    assert captured.value.delivery_state is DeliveryState.UNKNOWN


@pytest.mark.asyncio
async def test_feishu_workspace_uses_principal_scoped_token_provider() -> None:
    class TokenProvider:
        async def access_token(self, bound_principal: PrincipalIdentity) -> str:
            return f"token-for-{bound_principal.principal_id}"

    authorizations: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        authorizations.append(request.headers["authorization"])
        return httpx.Response(200, json={"code": 0, "data": {"items": []}})

    provider = FeishuWorkspaceProvider(
        base_url="https://open.feishu.cn",
        token_provider=TokenProvider(),
        calendar_id="calendar",
        transport=httpx.MockTransport(respond),
    )
    await provider.query(principal("first"), {})
    await provider.query(principal("second"), {})
    assert authorizations == ["Bearer token-for-first", "Bearer token-for-second"]
