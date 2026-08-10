from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Protocol

import aiosqlite

from ..kernel.intent import RiskLevel
from ..kernel.models import (
    AttachmentRef,
    ConversationRef,
    DeliveryTarget,
    IntentResult,
    PrincipalIdentity,
)
from ..ports.credentials import (
    OAuthAuthorizationState,
    OAuthCredential,
    OAuthCredentialRepository,
)
from ..ports.repositories import (
    ActionRecord,
    ActionRepository,
    ActionStatus,
    AttachmentRepository,
    AuditRepository,
    ConversationMessage,
    ConversationRepository,
    DeliveryBindingRepository,
    EventDedupRepository,
    ExecutionRecord,
    ExecutionRepository,
    ExecutionReservation,
    ExecutionStatus,
    MemoryRepository,
    ModuleRecordRepository,
    SchedulerLeaseClaim,
    SchedulerLeaseRepository,
)
from .database import Database


def _now() -> datetime:
    return datetime.now(UTC)


def _dump(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load(value: str | None) -> Any:
    return json.loads(value) if value else None


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _datetime(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


class SqliteExecutionRepository(ExecutionRepository):
    def __init__(self, database: Database) -> None:
        self.database = database

    async def reserve(
        self,
        *,
        execution_id: str,
        tenant_id: str,
        principal_id: str,
        intent_id: str,
        intent_version: int,
        idempotency_key: str,
        payload_hash: str,
    ) -> ExecutionReservation:
        now = _iso(_now())
        async with self.database.transaction(immediate=True) as connection:
            try:
                await connection.execute(
                    """
                    INSERT INTO executions(
                        execution_id, tenant_id, principal_id, intent_id, intent_version,
                        idempotency_key, payload_hash, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        execution_id,
                        tenant_id,
                        principal_id,
                        intent_id,
                        intent_version,
                        idempotency_key,
                        payload_hash,
                        ExecutionStatus.EXECUTING.value,
                        now,
                        now,
                    ),
                )
                row = await self._get_row(connection, execution_id)
                return ExecutionReservation("reserved", _execution_from_row(row))
            except aiosqlite.IntegrityError:
                row = await (
                    await connection.execute(
                        """
                        SELECT * FROM executions
                        WHERE tenant_id = ? AND principal_id = ? AND intent_id = ?
                          AND idempotency_key = ?
                        """,
                        (tenant_id, principal_id, intent_id, idempotency_key),
                    )
                ).fetchone()
                if row is None:
                    raise
                record = _execution_from_row(row)
                if record.payload_hash != payload_hash or record.intent_version != intent_version:
                    return ExecutionReservation("conflict", record)
                if record.status is ExecutionStatus.EXECUTING:
                    return ExecutionReservation("in_progress", record)
                return ExecutionReservation("replay", record)

    async def finish(
        self,
        execution_id: str,
        *,
        status: ExecutionStatus,
        result: IntentResult,
        provider_request_id: str = "",
    ) -> ExecutionRecord:
        async with self.database.transaction(immediate=True) as connection:
            cursor = await connection.execute(
                """
                UPDATE executions
                SET status = ?, result_json = ?, provider_request_id = ?, updated_at = ?
                WHERE execution_id = ? AND status = ?
                """,
                (
                    status.value,
                    result.model_dump_json(),
                    provider_request_id,
                    _iso(_now()),
                    execution_id,
                    ExecutionStatus.EXECUTING.value,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"execution is not executing: {execution_id}")
            row = await self._get_row(connection, execution_id)
        return _execution_from_row(row)

    async def get(self, execution_id: str) -> ExecutionRecord | None:
        async with self.database.connect() as connection:
            row = await self._get_row(connection, execution_id)
        return _execution_from_row(row) if row else None

    async def retry_unknown(self, execution_id: str) -> ExecutionRecord | None:
        async with self.database.transaction(immediate=True) as connection:
            cursor = await connection.execute(
                """
                UPDATE executions SET status = ?, updated_at = ?
                WHERE execution_id = ? AND status = ?
                """,
                (
                    ExecutionStatus.EXECUTING.value,
                    _iso(_now()),
                    execution_id,
                    ExecutionStatus.UNKNOWN.value,
                ),
            )
            if cursor.rowcount != 1:
                return None
            row = await self._get_row(connection, execution_id)
        return _execution_from_row(row)

    async def resolve_unknown(
        self,
        execution_id: str,
        *,
        status: ExecutionStatus,
        result: IntentResult,
    ) -> ExecutionRecord | None:
        if status not in {ExecutionStatus.SUCCEEDED, ExecutionStatus.FAILED}:
            raise ValueError("UNKNOWN execution can only resolve to SUCCEEDED or FAILED")
        async with self.database.transaction(immediate=True) as connection:
            cursor = await connection.execute(
                """
                UPDATE executions SET status = ?, result_json = ?, updated_at = ?
                WHERE execution_id = ? AND status = ?
                """,
                (
                    status.value,
                    result.model_dump_json(),
                    _iso(_now()),
                    execution_id,
                    ExecutionStatus.UNKNOWN.value,
                ),
            )
            if cursor.rowcount != 1:
                return None
            row = await self._get_row(connection, execution_id)
        return _execution_from_row(row)

    @staticmethod
    async def _get_row(connection: aiosqlite.Connection, execution_id: str) -> Any:
        return await (
            await connection.execute(
                "SELECT * FROM executions WHERE execution_id = ?", (execution_id,)
            )
        ).fetchone()


class SqliteActionRepository(ActionRepository):
    def __init__(self, database: Database) -> None:
        self.database = database

    async def create(self, action: ActionRecord) -> ActionRecord:
        async with self.database.transaction(immediate=True) as connection:
            try:
                await connection.execute(
                    """
                    INSERT INTO actions(
                        action_id, intent_id, intent_version, module_id, module_version,
                        arguments_json, principal_json, delivery_target_json,
                        requested_by_json, requested_tenant_id, requested_principal_id,
                        confirmed_by_json, risk_level, permissions_json, payload_hash,
                        idempotency_key, status, result_json, expires_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    _action_values(action),
                )
                return action
            except aiosqlite.IntegrityError:
                row = await (
                    await connection.execute(
                        """
                        SELECT * FROM actions
                        WHERE requested_tenant_id = ? AND requested_principal_id = ?
                          AND intent_id = ? AND idempotency_key = ?
                        """,
                        (
                            action.requested_by.tenant_id,
                            action.requested_by.principal_id,
                            action.intent_id,
                            action.idempotency_key,
                        ),
                    )
                ).fetchone()
                if row is None:
                    raise
                existing = _action_from_row(row)
                if existing.payload_hash != action.payload_hash:
                    raise RuntimeError(
                        "action idempotency key conflicts with another payload"
                    ) from None
                return existing

    async def get(self, action_id: str) -> ActionRecord | None:
        async with self.database.connect() as connection:
            row = await self._get_row(connection, action_id)
        return _action_from_row(row) if row else None

    async def claim(
        self,
        action_id: str,
        *,
        confirmed_by: PrincipalIdentity,
        now: datetime,
    ) -> ActionRecord | None:
        async with self.database.transaction(immediate=True) as connection:
            cursor = await connection.execute(
                """
                UPDATE actions
                SET status = ?, confirmed_by_json = ?, updated_at = ?
                WHERE action_id = ? AND status = ? AND expires_at > ?
                  AND requested_tenant_id = ? AND requested_principal_id = ?
                """,
                (
                    ActionStatus.EXECUTING.value,
                    _dump(confirmed_by),
                    _iso(now),
                    action_id,
                    ActionStatus.PENDING.value,
                    _iso(now),
                    confirmed_by.tenant_id,
                    confirmed_by.principal_id,
                ),
            )
            if cursor.rowcount != 1:
                await connection.execute(
                    """
                    UPDATE actions SET status = ?, updated_at = ?
                    WHERE action_id = ? AND status = ? AND expires_at <= ?
                    """,
                    (
                        ActionStatus.EXPIRED.value,
                        _iso(now),
                        action_id,
                        ActionStatus.PENDING.value,
                        _iso(now),
                    ),
                )
                return None
            row = await self._get_row(connection, action_id)
        return _action_from_row(row)

    async def finish(
        self,
        action_id: str,
        *,
        status: ActionStatus,
        result: IntentResult,
    ) -> ActionRecord:
        async with self.database.transaction(immediate=True) as connection:
            cursor = await connection.execute(
                """
                UPDATE actions SET status = ?, result_json = ?, updated_at = ?
                WHERE action_id = ? AND status = ?
                """,
                (
                    status.value,
                    result.model_dump_json(),
                    _iso(_now()),
                    action_id,
                    ActionStatus.EXECUTING.value,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"action is not executing: {action_id}")
            row = await self._get_row(connection, action_id)
        return _action_from_row(row)

    async def cancel(
        self,
        action_id: str,
        *,
        principal: PrincipalIdentity,
        now: datetime,
    ) -> ActionRecord | None:
        async with self.database.transaction(immediate=True) as connection:
            cursor = await connection.execute(
                """
                UPDATE actions SET status = ?, updated_at = ?
                WHERE action_id = ? AND status = ? AND expires_at > ?
                  AND requested_tenant_id = ? AND requested_principal_id = ?
                """,
                (
                    ActionStatus.CANCELLED.value,
                    _iso(now),
                    action_id,
                    ActionStatus.PENDING.value,
                    _iso(now),
                    principal.tenant_id,
                    principal.principal_id,
                ),
            )
            if cursor.rowcount != 1:
                return None
            row = await self._get_row(connection, action_id)
        return _action_from_row(row)

    async def claim_unknown_for_retry(
        self,
        action_id: str,
        *,
        principal: PrincipalIdentity,
    ) -> ActionRecord | None:
        async with self.database.transaction(immediate=True) as connection:
            cursor = await connection.execute(
                """
                UPDATE actions SET status = ?, updated_at = ?
                WHERE action_id = ? AND status = ?
                  AND requested_tenant_id = ? AND requested_principal_id = ?
                """,
                (
                    ActionStatus.EXECUTING.value,
                    _iso(_now()),
                    action_id,
                    ActionStatus.UNKNOWN.value,
                    principal.tenant_id,
                    principal.principal_id,
                ),
            )
            if cursor.rowcount != 1:
                return None
            row = await self._get_row(connection, action_id)
        return _action_from_row(row)

    async def resolve_unknown(
        self,
        action_id: str,
        *,
        status: ActionStatus,
        result: IntentResult,
    ) -> ActionRecord | None:
        if status not in {ActionStatus.SUCCEEDED, ActionStatus.FAILED}:
            raise ValueError("UNKNOWN can only be resolved to SUCCEEDED or FAILED")
        async with self.database.transaction(immediate=True) as connection:
            cursor = await connection.execute(
                """
                UPDATE actions SET status = ?, result_json = ?, updated_at = ?
                WHERE action_id = ? AND status = ?
                """,
                (
                    status.value,
                    result.model_dump_json(),
                    _iso(_now()),
                    action_id,
                    ActionStatus.UNKNOWN.value,
                ),
            )
            if cursor.rowcount != 1:
                return None
            row = await self._get_row(connection, action_id)
        return _action_from_row(row)

    @staticmethod
    async def _get_row(connection: aiosqlite.Connection, action_id: str) -> Any:
        return await (
            await connection.execute("SELECT * FROM actions WHERE action_id = ?", (action_id,))
        ).fetchone()


class SqliteAuditRepository(AuditRepository):
    def __init__(self, database: Database) -> None:
        self.database = database

    async def append(self, **values: Any) -> None:
        async with self.database.transaction() as connection:
            await connection.execute(
                """
                INSERT INTO audit_events(
                    trace_id, request_id, tenant_id, principal_id, channel, intent_id,
                    intent_version, module_id, phase, outcome, payload_hash,
                    metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    values["trace_id"],
                    values["request_id"],
                    values["tenant_id"],
                    values["principal_id"],
                    values["channel"],
                    values["intent_id"],
                    values["intent_version"],
                    values["module_id"],
                    values["phase"],
                    values["outcome"],
                    values["payload_hash"],
                    _dump(values["metadata"]),
                    _iso(_now()),
                ),
            )


class SqliteConversationRepository(ConversationRepository):
    def __init__(self, database: Database) -> None:
        self.database = database

    async def append_message(
        self,
        *,
        conversation: ConversationRef,
        message_id: str,
        role: str,
        content: str,
    ) -> bool:
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                """
                INSERT INTO conversation_messages(
                    message_id, conversation_key, role, content, created_at
                ) VALUES (?, ?, ?, ?, ?) ON CONFLICT(message_id) DO NOTHING
                """,
                (message_id, conversation.key, role, content, _iso(_now())),
            )
        return cursor.rowcount == 1

    async def recent_messages(
        self, conversation: ConversationRef, *, limit: int
    ) -> tuple[ConversationMessage, ...]:
        async with self.database.connect() as connection:
            rows = await (
                await connection.execute(
                    """
                    SELECT * FROM conversation_messages WHERE conversation_key = ?
                    ORDER BY created_at DESC LIMIT ?
                    """,
                    (conversation.key, limit),
                )
            ).fetchall()
        return tuple(
            ConversationMessage(
                message_id=row["message_id"],
                conversation_key=row["conversation_key"],
                role=row["role"],
                content=row["content"],
                created_at=_datetime(row["created_at"]),
            )
            for row in reversed(rows)
        )

    async def get_summary(self, conversation: ConversationRef) -> str:
        async with self.database.connect() as connection:
            row = await (
                await connection.execute(
                    "SELECT summary FROM conversation_summaries WHERE conversation_key = ?",
                    (conversation.key,),
                )
            ).fetchone()
        return row["summary"] if row else ""

    async def save_summary(self, conversation: ConversationRef, summary: str) -> None:
        async with self.database.transaction() as connection:
            await connection.execute(
                """
                INSERT INTO conversation_summaries(conversation_key, summary, updated_at)
                VALUES (?, ?, ?) ON CONFLICT(conversation_key) DO UPDATE SET
                    summary = excluded.summary, updated_at = excluded.updated_at
                """,
                (conversation.key, summary, _iso(_now())),
            )


class SqliteMemoryRepository(MemoryRepository):
    def __init__(self, database: Database) -> None:
        self.database = database

    async def list_memories(
        self, principal: PrincipalIdentity, *, limit: int = 20
    ) -> dict[str, str]:
        async with self.database.connect() as connection:
            rows = await (
                await connection.execute(
                    """
                    SELECT memory_key, value FROM memories
                    WHERE tenant_id = ? AND principal_id = ?
                    ORDER BY updated_at DESC LIMIT ?
                    """,
                    (principal.tenant_id, principal.principal_id, limit),
                )
            ).fetchall()
        return {row["memory_key"]: row["value"] for row in rows}

    async def put_memory(
        self, principal: PrincipalIdentity, *, key: str, value: str
    ) -> None:
        async with self.database.transaction() as connection:
            await connection.execute(
                """
                INSERT INTO memories(tenant_id, principal_id, memory_key, value, updated_at)
                VALUES (?, ?, ?, ?, ?) ON CONFLICT(tenant_id, principal_id, memory_key)
                DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (principal.tenant_id, principal.principal_id, key, value, _iso(_now())),
            )


class SqliteAttachmentRepository(AttachmentRepository):
    def __init__(self, database: Database) -> None:
        self.database = database

    async def create(
        self,
        *,
        attachment: AttachmentRef,
        principal: PrincipalIdentity,
        conversation: ConversationRef | None,
        storage_key: str,
        temporary: bool,
    ) -> None:
        async with self.database.transaction() as connection:
            await connection.execute(
                """
                INSERT INTO attachments(
                    attachment_id, tenant_id, principal_id, conversation_key, media_type,
                    filename, size_bytes, sha256, expires_at, storage_key, temporary, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attachment.attachment_id,
                    principal.tenant_id,
                    principal.principal_id,
                    conversation.key if conversation else "",
                    attachment.media_type,
                    attachment.filename,
                    attachment.size_bytes,
                    attachment.sha256,
                    _iso(attachment.expires_at) if attachment.expires_at else None,
                    storage_key,
                    temporary,
                    _iso(_now()),
                ),
            )

    async def get_owned(
        self,
        attachment_id: str,
        *,
        principal: PrincipalIdentity,
        conversation: ConversationRef | None,
    ) -> tuple[AttachmentRef, str] | None:
        async with self.database.connect() as connection:
            row = await (
                await connection.execute(
                    """
                    SELECT * FROM attachments
                    WHERE attachment_id = ? AND tenant_id = ? AND principal_id = ?
                      AND (conversation_key = '' OR conversation_key = ?)
                    """,
                    (
                        attachment_id,
                        principal.tenant_id,
                        principal.principal_id,
                        conversation.key if conversation else "",
                    ),
                )
            ).fetchone()
        if row is None:
            return None
        expires_at = _datetime(row["expires_at"]) if row["expires_at"] else None
        if expires_at is not None and expires_at <= _now():
            return None
        return (
            AttachmentRef(
                attachment_id=row["attachment_id"],
                media_type=row["media_type"],
                filename=row["filename"],
                size_bytes=row["size_bytes"],
                sha256=row["sha256"],
                expires_at=expires_at,
            ),
            row["storage_key"],
        )


class SqliteDeliveryBindingRepository(DeliveryBindingRepository):
    def __init__(self, database: Database) -> None:
        self.database = database

    async def resolve(
        self, binding_id: str, *, principal: PrincipalIdentity
    ) -> DeliveryTarget | None:
        async with self.database.connect() as connection:
            row = await (
                await connection.execute(
                    """
                    SELECT target_json FROM delivery_bindings
                    WHERE binding_id = ? AND tenant_id = ? AND principal_id = ?
                    """,
                    (binding_id, principal.tenant_id, principal.principal_id),
                )
            ).fetchone()
        return DeliveryTarget.model_validate_json(row["target_json"]) if row else None

    async def save(
        self,
        *,
        binding_id: str,
        principal: PrincipalIdentity,
        target: DeliveryTarget,
    ) -> None:
        async with self.database.transaction() as connection:
            await connection.execute(
                """
                INSERT INTO delivery_bindings(
                    binding_id, tenant_id, principal_id, target_json, updated_at
                ) VALUES (?, ?, ?, ?, ?) ON CONFLICT(binding_id) DO UPDATE SET
                    tenant_id = excluded.tenant_id,
                    principal_id = excluded.principal_id,
                    target_json = excluded.target_json,
                    updated_at = excluded.updated_at
                """,
                (
                    binding_id,
                    principal.tenant_id,
                    principal.principal_id,
                    target.model_dump_json(),
                    _iso(_now()),
                ),
            )


class SqliteModuleRecordRepository(ModuleRecordRepository):
    def __init__(self, database: Database) -> None:
        self.database = database

    async def get(
        self,
        *,
        module_id: str,
        record_type: str,
        record_id: str,
        principal: PrincipalIdentity,
    ) -> dict[str, Any] | None:
        async with self.database.connect() as connection:
            row = await (
                await connection.execute(
                    """
                    SELECT payload_json FROM module_records
                    WHERE module_id = ? AND record_type = ? AND record_id = ?
                      AND tenant_id = ? AND principal_id = ?
                    """,
                    (
                        module_id,
                        record_type,
                        record_id,
                        principal.tenant_id,
                        principal.principal_id,
                    ),
                )
            ).fetchone()
        return _load(row["payload_json"]) if row else None

    async def put(
        self,
        *,
        module_id: str,
        record_type: str,
        record_id: str,
        principal: PrincipalIdentity,
        payload: dict[str, Any],
    ) -> None:
        now = _iso(_now())
        async with self.database.transaction(immediate=True) as connection:
            await connection.execute(
                """
                INSERT INTO module_records(
                    module_id, record_type, record_id, tenant_id, principal_id,
                    payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(
                    module_id, record_type, record_id
                ) DO UPDATE SET
                    tenant_id = excluded.tenant_id,
                    principal_id = excluded.principal_id,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    module_id,
                    record_type,
                    record_id,
                    principal.tenant_id,
                    principal.principal_id,
                    _dump(payload),
                    now,
                    now,
                ),
            )

    async def delete(
        self,
        *,
        module_id: str,
        record_type: str,
        record_id: str,
        principal: PrincipalIdentity,
    ) -> bool:
        async with self.database.transaction(immediate=True) as connection:
            cursor = await connection.execute(
                """
                DELETE FROM module_records
                WHERE module_id = ? AND record_type = ? AND record_id = ?
                  AND tenant_id = ? AND principal_id = ?
                """,
                (
                    module_id,
                    record_type,
                    record_id,
                    principal.tenant_id,
                    principal.principal_id,
                ),
            )
        return cursor.rowcount == 1


class SqliteEventDedupRepository(EventDedupRepository):
    def __init__(self, database: Database) -> None:
        self.database = database

    async def claim(self, event_id: str, *, source: str) -> bool:
        async with self.database.transaction(immediate=True) as connection:
            cursor = await connection.execute(
                """
                INSERT INTO processed_events(event_id, source, processed_at)
                VALUES (?, ?, ?) ON CONFLICT(event_id) DO NOTHING
                """,
                (event_id, source, _iso(_now())),
            )
        return cursor.rowcount == 1


class SqliteSchedulerLeaseRepository(SchedulerLeaseRepository):
    def __init__(self, database: Database) -> None:
        self.database = database

    async def claim(
        self,
        job_id: str,
        *,
        owner_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> SchedulerLeaseClaim | None:
        from datetime import timedelta

        lease_until = now + timedelta(seconds=lease_seconds)
        async with self.database.transaction(immediate=True) as connection:
            row = await (
                await connection.execute(
                    "SELECT * FROM scheduler_leases WHERE job_id = ?", (job_id,)
                )
            ).fetchone()
            if row is None:
                token = 1
                await connection.execute(
                    """
                    INSERT INTO scheduler_leases(
                        job_id, owner_id, lease_until, fencing_token, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (job_id, owner_id, _iso(lease_until), token, _iso(now)),
                )
            elif row["owner_id"] == owner_id or _datetime(row["lease_until"]) <= now:
                token = int(row["fencing_token"]) + 1
                await connection.execute(
                    """
                    UPDATE scheduler_leases SET owner_id = ?, lease_until = ?,
                        fencing_token = ?, updated_at = ? WHERE job_id = ?
                    """,
                    (owner_id, _iso(lease_until), token, _iso(now), job_id),
                )
            else:
                return None
        return SchedulerLeaseClaim(job_id, owner_id, token, lease_until)


class _Cipher(Protocol):
    def encrypt(self, plaintext: bytes, *, associated_data: bytes) -> tuple[bytes, bytes]: ...

    def decrypt(self, nonce: bytes, ciphertext: bytes, *, associated_data: bytes) -> bytes: ...


class SqliteOAuthCredentialRepository(OAuthCredentialRepository):
    def __init__(self, database: Database, cipher: _Cipher) -> None:
        self.database = database
        self.cipher = cipher

    async def get(
        self, principal: PrincipalIdentity, *, provider_id: str
    ) -> OAuthCredential | None:
        async with self.database.connect() as connection:
            row = await (
                await connection.execute(
                    """
                    SELECT * FROM oauth_tokens
                    WHERE tenant_id = ? AND principal_id = ? AND provider_id = ?
                    """,
                    (principal.tenant_id, principal.principal_id, provider_id),
                )
            ).fetchone()
        if row is None:
            return None
        plaintext = self.cipher.decrypt(
            row["nonce"],
            row["ciphertext"],
            associated_data=_credential_aad(principal, provider_id),
        )
        value = json.loads(plaintext)
        return OAuthCredential(
            access_token=value["access_token"],
            refresh_token=value.get("refresh_token", ""),
            scopes=frozenset(_load(row["scopes_json"])),
            expires_at=_datetime(row["expires_at"]),
            refresh_expires_at=_datetime(value["refresh_expires_at"]),
        )

    async def save(
        self,
        principal: PrincipalIdentity,
        credential: OAuthCredential,
        *,
        provider_id: str,
    ) -> None:
        plaintext = _dump(
            {
                "access_token": credential.access_token,
                "refresh_token": credential.refresh_token,
                "refresh_expires_at": _iso(credential.refresh_expires_at),
            }
        ).encode()
        nonce, ciphertext = self.cipher.encrypt(
            plaintext, associated_data=_credential_aad(principal, provider_id)
        )
        async with self.database.transaction(immediate=True) as connection:
            await connection.execute(
                """
                INSERT INTO oauth_tokens(
                    tenant_id, principal_id, provider_id, ciphertext, nonce,
                    scopes_json, expires_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(
                    tenant_id, principal_id, provider_id
                ) DO UPDATE SET
                    ciphertext = excluded.ciphertext,
                    nonce = excluded.nonce,
                    scopes_json = excluded.scopes_json,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                (
                    principal.tenant_id,
                    principal.principal_id,
                    provider_id,
                    ciphertext,
                    nonce,
                    _dump(sorted(credential.scopes)),
                    _iso(credential.expires_at),
                    _iso(_now()),
                ),
            )

    async def save_state(self, state: str, value: OAuthAuthorizationState) -> None:
        async with self.database.transaction(immediate=True) as connection:
            await connection.execute(
                """
                INSERT INTO oauth_states(
                    state_hash, principal_json, delivery_target_json,
                    required_scopes_json, expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    _state_hash(state),
                    value.principal.model_dump_json(),
                    value.delivery_target.model_dump_json(),
                    _dump(sorted(value.required_scopes)),
                    _iso(value.expires_at),
                    _iso(_now()),
                ),
            )

    async def consume_state(self, state: str) -> OAuthAuthorizationState | None:
        now = _now()
        async with self.database.transaction(immediate=True) as connection:
            row = await (
                await connection.execute(
                    "SELECT * FROM oauth_states WHERE state_hash = ?",
                    (_state_hash(state),),
                )
            ).fetchone()
            await connection.execute(
                "DELETE FROM oauth_states WHERE state_hash = ? OR expires_at <= ?",
                (_state_hash(state), _iso(now)),
            )
        if row is None or _datetime(row["expires_at"]) <= now:
            return None
        return OAuthAuthorizationState(
            principal=PrincipalIdentity.model_validate_json(row["principal_json"]),
            delivery_target=DeliveryTarget.model_validate_json(
                row["delivery_target_json"]
            ),
            required_scopes=frozenset(_load(row["required_scopes_json"])),
            expires_at=_datetime(row["expires_at"]),
        )


def _credential_aad(principal: PrincipalIdentity, provider_id: str) -> bytes:
    return "\x1f".join(
        ("song-agent.oauth.v1", principal.tenant_id, principal.principal_id, provider_id)
    ).encode()


def _state_hash(state: str) -> str:
    return hashlib.sha256(state.encode()).hexdigest()


def _execution_from_row(row: Any) -> ExecutionRecord:
    return ExecutionRecord(
        execution_id=row["execution_id"],
        tenant_id=row["tenant_id"],
        principal_id=row["principal_id"],
        intent_id=row["intent_id"],
        intent_version=row["intent_version"],
        idempotency_key=row["idempotency_key"],
        payload_hash=row["payload_hash"],
        status=ExecutionStatus(row["status"]),
        result=IntentResult.model_validate_json(row["result_json"])
        if row["result_json"]
        else None,
        provider_request_id=row["provider_request_id"],
        created_at=_datetime(row["created_at"]),
        updated_at=_datetime(row["updated_at"]),
    )


def _action_values(action: ActionRecord) -> tuple[Any, ...]:
    return (
        action.action_id,
        action.intent_id,
        action.intent_version,
        action.module_id,
        action.module_version,
        _dump(action.arguments),
        _dump(action.principal),
        _dump(action.delivery_target),
        _dump(action.requested_by),
        action.requested_by.tenant_id,
        action.requested_by.principal_id,
        _dump(action.confirmed_by) if action.confirmed_by else None,
        action.risk_level.value,
        _dump(sorted(action.permissions)),
        action.payload_hash,
        action.idempotency_key,
        action.status.value,
        action.result.model_dump_json() if action.result else None,
        _iso(action.expires_at),
        _iso(action.created_at),
        _iso(action.updated_at),
    )


def _action_from_row(row: Any) -> ActionRecord:
    return ActionRecord(
        action_id=row["action_id"],
        intent_id=row["intent_id"],
        intent_version=row["intent_version"],
        module_id=row["module_id"],
        module_version=row["module_version"],
        arguments=_load(row["arguments_json"]),
        principal=PrincipalIdentity.model_validate_json(row["principal_json"]),
        delivery_target=DeliveryTarget.model_validate_json(row["delivery_target_json"]),
        requested_by=PrincipalIdentity.model_validate_json(row["requested_by_json"]),
        confirmed_by=PrincipalIdentity.model_validate_json(row["confirmed_by_json"])
        if row["confirmed_by_json"]
        else None,
        risk_level=RiskLevel(row["risk_level"]),
        permissions=frozenset(_load(row["permissions_json"])),
        payload_hash=row["payload_hash"],
        idempotency_key=row["idempotency_key"],
        status=ActionStatus(row["status"]),
        result=IntentResult.model_validate_json(row["result_json"])
        if row["result_json"]
        else None,
        expires_at=_datetime(row["expires_at"]),
        created_at=_datetime(row["created_at"]),
        updated_at=_datetime(row["updated_at"]),
    )
