"""Persistent access helpers for API idempotency and Feishu channel bindings."""

from __future__ import annotations

import hashlib
import secrets

from ..models import ApiChannelBinding, FeishuIdentity
from ..store import SqliteStore


def binding_code_hash(code: str) -> str:
    return hashlib.sha256(code.strip().upper().encode()).hexdigest()


class ApiAccessService:
    def __init__(
        self,
        store: SqliteStore,
        *,
        api_app_id: str,
        binding_code_ttl_seconds: int,
    ) -> None:
        self.store = store
        self.api_app_id = api_app_id
        self.binding_code_ttl_seconds = binding_code_ttl_seconds

    async def create_binding_code(
        self,
        *,
        tenant_key: str,
        principal_id: str,
    ) -> tuple[str, int]:
        code = secrets.token_hex(6).upper()
        expires_at = await self.store.create_api_binding_code(
            code_hash=binding_code_hash(code),
            tenant_key=tenant_key,
            app_id=self.api_app_id,
            principal_id=principal_id,
            ttl_seconds=self.binding_code_ttl_seconds,
        )
        return code, expires_at

    async def redeem_binding_code(
        self,
        code: str,
        *,
        identity: FeishuIdentity,
        chat_id: str,
    ) -> ApiChannelBinding | None:
        return await self.store.redeem_api_binding_code(
            code_hash=binding_code_hash(code),
            identity=identity,
            chat_id=chat_id,
        )
