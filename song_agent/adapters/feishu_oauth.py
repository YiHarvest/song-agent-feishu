from __future__ import annotations

import asyncio
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx

from ..kernel.errors import DeliveryState, ProviderError
from ..kernel.models import DeliveryTarget, PrincipalIdentity
from ..ports.credentials import (
    FeishuTokenPort,
    OAuthAuthorizationState,
    OAuthCredential,
    OAuthCredentialRepository,
)


class FeishuOAuthService(FeishuTokenPort):
    PROVIDER_ID = "feishu"

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        base_url: str,
        public_base_url: str,
        repository: OAuthCredentialRepository,
        permission_scope_map: dict[str, str | tuple[str, ...]],
        timeout: float = 20,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.base_url = base_url.rstrip("/")
        self.public_base_url = public_base_url.rstrip("/")
        self.repository = repository
        self.permission_scope_map = permission_scope_map
        self.timeout = timeout
        self._refresh_locks: dict[str, asyncio.Lock] = {}
        self.transport: httpx.AsyncBaseTransport | None = None

    async def ensure_authorized(
        self,
        principal: PrincipalIdentity,
        target: DeliveryTarget,
        permissions: frozenset[str],
    ) -> str:
        if not permissions:
            return ""
        required_scopes = frozenset(
            scope
            for permission in permissions
            for scope in _scope_values(self.permission_scope_map.get(permission))
        )
        if not required_scopes:
            return ""
        credential = await self._valid_credential(principal)
        if credential is not None and required_scopes <= credential.scopes:
            return ""
        return await self.create_authorization_url(principal, target, required_scopes)

    async def access_token(self, principal: PrincipalIdentity) -> str:
        credential = await self._valid_credential(principal)
        if credential is None:
            raise ProviderError(
                "feishu.oauth_required",
                "Feishu user authorization is required.",
                delivery_state=DeliveryState.NOT_SENT,
            )
        return credential.access_token

    async def create_authorization_url(
        self,
        principal: PrincipalIdentity,
        target: DeliveryTarget,
        required_scopes: frozenset[str],
    ) -> str:
        state = secrets.token_urlsafe(32)
        await self.repository.save_state(
            state,
            OAuthAuthorizationState(
                principal=principal,
                delivery_target=target,
                required_scopes=required_scopes,
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            ),
        )
        query = urlencode(
            {
                "client_id": self.app_id,
                "redirect_uri": f"{self.public_base_url}/adapters/feishu/oauth/callback",
                "scope": " ".join(sorted({"offline_access", *required_scopes})),
                "state": state,
            }
        )
        return f"{self.base_url}/open-apis/authen/v1/authorize?{query}"

    async def callback(self, *, code: str, state: str) -> DeliveryTarget:
        pending = await self.repository.consume_state(state)
        if pending is None:
            raise ValueError("OAuth state is invalid or expired")
        payload = await self._exchange(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": f"{self.public_base_url}/adapters/feishu/oauth/callback",
            }
        )
        actual_open_id = await self._current_open_id(payload["access_token"])
        if actual_open_id != pending.principal.principal_id:
            raise PermissionError("authorized Feishu account does not match the requester")
        credential = _credential_from_payload(payload)
        if not pending.required_scopes <= credential.scopes:
            raise PermissionError("authorized token is missing required scopes")
        await self.repository.save(
            pending.principal,
            credential,
            provider_id=self.PROVIDER_ID,
        )
        return pending.delivery_target

    async def _valid_credential(
        self, principal: PrincipalIdentity
    ) -> OAuthCredential | None:
        credential = await self.repository.get(principal, provider_id=self.PROVIDER_ID)
        now = datetime.now(UTC)
        if credential is None:
            return None
        if credential.expires_at > now + timedelta(minutes=1):
            return credential
        if not credential.refresh_token or credential.refresh_expires_at <= now:
            return None
        key = f"{principal.tenant_id}:{principal.principal_id}"
        lock = self._refresh_locks.setdefault(key, asyncio.Lock())
        async with lock:
            current = await self.repository.get(principal, provider_id=self.PROVIDER_ID)
            now = datetime.now(UTC)
            if current is None:
                return None
            if current.expires_at > now + timedelta(minutes=1):
                return current
            payload = await self._exchange(
                {"grant_type": "refresh_token", "refresh_token": current.refresh_token}
            )
            refreshed = _credential_from_payload(payload, fallback_refresh=current)
            await self.repository.save(
                principal,
                refreshed,
                provider_id=self.PROVIDER_ID,
            )
            return refreshed

    async def _exchange(self, fields: dict[str, str]) -> dict:
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                transport=self.transport,
                trust_env=False,
            ) as client:
                response = await client.post(
                    "/open-apis/authen/v2/oauth/token",
                    json={
                        "client_id": self.app_id,
                        "client_secret": self.app_secret,
                        **fields,
                    },
                )
        except httpx.RequestError as error:
            raise ProviderError(
                "feishu.oauth_unavailable",
                "Unable to reach Feishu OAuth.",
                retryable=True,
                delivery_state=DeliveryState.NOT_SENT,
            ) from error
        try:
            payload = response.json() if response.content else {}
        except ValueError as error:
            raise ProviderError(
                "feishu.oauth_invalid_response",
                "Feishu OAuth returned an invalid response.",
                delivery_state=DeliveryState.NOT_SENT,
            ) from error
        if response.is_error or payload.get("code") or not payload.get("access_token"):
            raise ProviderError(
                "feishu.oauth_exchange_failed",
                str(payload.get("msg") or "Feishu OAuth token exchange failed."),
                delivery_state=DeliveryState.NOT_SENT,
            )
        return payload

    async def _current_open_id(self, access_token: str) -> str:
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                transport=self.transport,
                trust_env=False,
            ) as client:
                response = await client.get(
                    "/open-apis/authen/v1/user_info",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
        except httpx.RequestError as error:
            raise ProviderError(
                "feishu.oauth_user_info_unavailable",
                "Unable to verify the authorized Feishu account.",
                retryable=True,
                delivery_state=DeliveryState.NOT_SENT,
            ) from error
        try:
            payload = response.json() if response.content else {}
        except ValueError as error:
            raise ProviderError(
                "feishu.oauth_user_info_invalid_response",
                "Feishu returned an invalid user information response.",
                delivery_state=DeliveryState.NOT_SENT,
            ) from error
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        open_id = str(data.get("open_id") or "")
        if response.is_error or payload.get("code") or not open_id:
            raise ProviderError(
                "feishu.oauth_user_info_failed",
                "Unable to verify the authorized Feishu account.",
                delivery_state=DeliveryState.NOT_SENT,
            )
        return open_id


def _credential_from_payload(
    payload: dict,
    *,
    fallback_refresh: OAuthCredential | None = None,
) -> OAuthCredential:
    now = datetime.now(UTC)
    refresh_token = str(payload.get("refresh_token") or "")
    refresh_expires = int(payload.get("refresh_token_expires_in") or 0)
    if not refresh_token and fallback_refresh is not None:
        refresh_token = fallback_refresh.refresh_token
        refresh_expires_at = fallback_refresh.refresh_expires_at
    else:
        refresh_expires_at = now + timedelta(seconds=refresh_expires or 30 * 86400)
    scopes = frozenset(
        item for item in str(payload.get("scope") or "").replace(",", " ").split() if item
    )
    if not scopes and fallback_refresh is not None:
        scopes = fallback_refresh.scopes
    return OAuthCredential(
        access_token=str(payload["access_token"]),
        refresh_token=refresh_token,
        scopes=scopes,
        expires_at=now + timedelta(seconds=int(payload.get("expires_in") or 7200)),
        refresh_expires_at=refresh_expires_at,
    )


def _scope_values(value: str | tuple[str, ...] | None) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return value
