"""只追加审计服务。"""

from __future__ import annotations

from typing import Any

from ..observability.context import current_trace_id
from ..observability.redaction import redact
from ..store import SqliteStore


class AuditService:
    def __init__(self, store: SqliteStore) -> None:
        self.store = store

    async def record(
        self,
        operation: str,
        result: str,
        *,
        tenant_key: str = "",
        app_id: str = "",
        principal_id: str = "",
        chat_id: str = "",
        thread_id: str = "",
        message_id: str = "",
        agent_run_id: str = "",
        action_id: str = "",
        decision: str = "",
        risk_level: str = "",
        payload_hash: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await self.store.write_audit_log(
            trace_id=current_trace_id(),
            operation=operation,
            result=result,
            tenant_key=tenant_key,
            app_id=app_id,
            principal_id=principal_id,
            chat_id=chat_id,
            thread_id=thread_id,
            message_id=message_id,
            agent_run_id=agent_run_id,
            action_id=action_id,
            decision=decision,
            risk_level=risk_level,
            payload_hash=payload_hash,
            metadata=redact(metadata or {}),
        )
