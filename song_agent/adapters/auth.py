from __future__ import annotations

import hashlib
import hmac
from typing import Protocol

from ..kernel.models import DeliveryTarget, PrincipalIdentity
from ..ports.auth import AuthorizationDecision


class ApiKeyAuthenticator:
    def __init__(self, *, api_key: str, tenant_id: str, principal_id: str) -> None:
        self._digest = hashlib.sha256(api_key.encode()).digest()
        self.tenant_id = tenant_id
        self.principal_id = principal_id

    def authenticate(self, credential: str) -> PrincipalIdentity | None:
        candidate = hashlib.sha256(credential.encode()).digest()
        if not hmac.compare_digest(candidate, self._digest):
            return None
        return PrincipalIdentity(
            tenant_id=self.tenant_id,
            principal_id=self.principal_id,
            auth_provider="api_key",
        )


class OAuthAuthorizationPort(Protocol):
    async def ensure_authorized(
        self,
        principal: PrincipalIdentity,
        target: DeliveryTarget,
        permissions: frozenset[str],
    ) -> str: ...


class SemanticAuthorization:
    """First-stage authorization: trust authenticated API principals and channel adapters."""

    FEISHU_SCOPE_MAP = {
        "calendar.read": "calendar:calendar:readonly",
        "calendar.write": "calendar:calendar",
        "task.read": "task:task:read",
        "task.write": "task:task:write",
        "document.read": "docx:document:readonly",
        "document.write": ("docx:document", "drive:drive"),
    }

    def __init__(self, oauth: OAuthAuthorizationPort | None = None) -> None:
        self.oauth = oauth

    async def authorize(
        self,
        principal: PrincipalIdentity,
        permissions: frozenset[str],
        delivery_target: DeliveryTarget,
    ) -> AuthorizationDecision:
        if not permissions:
            return AuthorizationDecision(True, granted_permissions=frozenset())
        if principal.auth_provider == "api_key":
            return AuthorizationDecision(True, granted_permissions=permissions)
        declared = frozenset(
            item for item in principal.attributes.get("permissions", "").split(",") if item
        )
        missing = permissions - declared
        if missing:
            return AuthorizationDecision(
                False,
                reason_code="authorization.permissions_missing",
                authorization_url=str(principal.attributes.get("authorization_url", "")),
                granted_permissions=permissions - missing,
            )
        if principal.auth_provider == "feishu_event" and self.oauth is not None:
            authorization_url = await self.oauth.ensure_authorized(
                principal, delivery_target, permissions
            )
            if authorization_url:
                return AuthorizationDecision(
                    False,
                    reason_code="authorization.feishu_oauth_required",
                    authorization_url=authorization_url,
                )
            return AuthorizationDecision(True, granted_permissions=permissions)
        return AuthorizationDecision(True, granted_permissions=permissions)
