from __future__ import annotations

import base64
import sqlite3
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from song_agent.adapters.auth import SemanticAuthorization
from song_agent.adapters.encryption import SingleKeyCipher
from song_agent.adapters.feishu_oauth import FeishuOAuthService
from song_agent.infrastructure.database import Database
from song_agent.infrastructure.repositories import SqliteOAuthCredentialRepository
from song_agent.kernel.models import DeliveryTarget, PrincipalIdentity
from song_agent.ports.credentials import OAuthAuthorizationState, OAuthCredential


def _principal() -> PrincipalIdentity:
    return PrincipalIdentity(
        tenant_id="tenant-1",
        principal_id="open-id-1",
        auth_provider="feishu_event",
    )


def _target() -> DeliveryTarget:
    return DeliveryTarget(
        channel="feishu",
        channel_account_id="app-1",
        destination_id="chat-1",
    )


def _repository(database_path) -> SqliteOAuthCredentialRepository:
    key = base64.urlsafe_b64encode(b"o" * 32).decode()
    return SqliteOAuthCredentialRepository(Database(database_path), SingleKeyCipher(key))


@pytest.mark.asyncio
async def test_oauth_repository_encrypts_credentials_and_consumes_state_once(database_path) -> None:
    repository = _repository(database_path)
    principal = _principal()
    now = datetime.now(UTC)
    credential = OAuthCredential(
        access_token="access-secret",
        refresh_token="refresh-secret",
        scopes=frozenset({"calendar:calendar.event:read"}),
        expires_at=now + timedelta(hours=1),
        refresh_expires_at=now + timedelta(days=30),
    )
    await repository.save(principal, credential, provider_id="feishu")

    with sqlite3.connect(database_path) as connection:
        ciphertext = connection.execute("SELECT ciphertext FROM oauth_tokens").fetchone()[0]
    assert b"access-secret" not in ciphertext
    assert b"refresh-secret" not in ciphertext
    assert await repository.get(principal, provider_id="feishu") == credential

    state_value = OAuthAuthorizationState(
        principal=principal,
        delivery_target=_target(),
        required_scopes=credential.scopes,
        expires_at=now + timedelta(minutes=10),
    )
    await repository.save_state("single-use-state", state_value)
    assert await repository.consume_state("single-use-state") == state_value
    assert await repository.consume_state("single-use-state") is None


@pytest.mark.asyncio
async def test_oauth_callback_binds_verified_feishu_identity(database_path) -> None:
    repository = _repository(database_path)

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/token"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "access_token": "access-token",
                    "refresh_token": "refresh-token",
                    "scope": "calendar:calendar.event:read offline_access",
                    "expires_in": 7200,
                    "refresh_token_expires_in": 2_592_000,
                },
            )
        if request.url.path.endswith("/user_info"):
            assert request.headers["authorization"] == "Bearer access-token"
            return httpx.Response(200, json={"code": 0, "data": {"open_id": "open-id-1"}})
        raise AssertionError(f"unexpected request: {request.url}")

    service = FeishuOAuthService(
        app_id="app-1",
        app_secret="app-secret",
        base_url="https://open.feishu.cn",
        public_base_url="https://agent.example",
        repository=repository,
        permission_scope_map={"calendar.read": "calendar:calendar.event:read"},
    )
    service.transport = httpx.MockTransport(respond)
    authorization_url = await service.create_authorization_url(
        _principal(), _target(), frozenset({"calendar:calendar.event:read"})
    )
    query = parse_qs(urlparse(authorization_url).query)
    assert query["redirect_uri"] == ["https://agent.example/adapters/feishu/oauth/callback"]

    target = await service.callback(code="authorization-code", state=query["state"][0])
    assert target == _target()
    assert await service.access_token(_principal()) == "access-token"


@pytest.mark.asyncio
async def test_no_permission_intent_does_not_require_feishu_oauth() -> None:
    class OAuthMustNotRun:
        async def ensure_authorized(self, principal, target, permissions):
            raise AssertionError("OAuth should not run when an intent requires no permission")

    decision = await SemanticAuthorization(OAuthMustNotRun()).authorize(
        _principal(), frozenset(), _target()
    )
    assert decision.granted
    assert decision.granted_permissions == frozenset()


@pytest.mark.asyncio
async def test_feishu_server_permission_allowlist_precedes_oauth() -> None:
    class OAuthMustNotRun:
        async def ensure_authorized(self, principal, target, permissions):
            raise AssertionError("OAuth cannot expand the server-side permission allowlist")

    decision = await SemanticAuthorization(OAuthMustNotRun()).authorize(
        _principal(), frozenset({"calendar.write"}), _target()
    )
    assert not decision.granted
    assert decision.reason_code == "authorization.permissions_missing"
