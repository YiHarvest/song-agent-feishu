from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from ..kernel.models import DeliveryTarget, PrincipalIdentity


@dataclass(frozen=True, slots=True)
class OAuthCredential:
    access_token: str
    refresh_token: str
    scopes: frozenset[str]
    expires_at: datetime
    refresh_expires_at: datetime


@dataclass(frozen=True, slots=True)
class OAuthAuthorizationState:
    principal: PrincipalIdentity
    delivery_target: DeliveryTarget
    required_scopes: frozenset[str]
    expires_at: datetime


class OAuthCredentialRepository(Protocol):
    async def get(
        self, principal: PrincipalIdentity, *, provider_id: str
    ) -> OAuthCredential | None: ...

    async def save(
        self,
        principal: PrincipalIdentity,
        credential: OAuthCredential,
        *,
        provider_id: str,
    ) -> None: ...

    async def save_state(self, state: str, value: OAuthAuthorizationState) -> None: ...

    async def consume_state(self, state: str) -> OAuthAuthorizationState | None: ...


class FeishuTokenPort(Protocol):
    async def access_token(self, principal: PrincipalIdentity) -> str: ...
