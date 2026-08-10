from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..kernel.models import DeliveryTarget, PrincipalIdentity


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    granted: bool
    granted_permissions: frozenset[str] = frozenset()
    authorization_url: str = ""
    reason_code: str = ""


class AuthorizationPort(Protocol):
    async def authorize(
        self,
        principal: PrincipalIdentity,
        permissions: frozenset[str],
        delivery_target: DeliveryTarget,
    ) -> AuthorizationDecision: ...
