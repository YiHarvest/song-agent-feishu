from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from ..kernel.intent import RiskLevel
from ..kernel.models import (
    AttachmentRef,
    ConversationRef,
    DeliveryTarget,
    IntentResult,
    PrincipalIdentity,
)


class ExecutionStatus(StrEnum):
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ActionStatus(StrEnum):
    PENDING = "pending"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class ExecutionRecord:
    execution_id: str
    tenant_id: str
    principal_id: str
    intent_id: str
    intent_version: int
    idempotency_key: str
    payload_hash: str
    status: ExecutionStatus
    result: IntentResult | None
    provider_request_id: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ExecutionReservation:
    outcome: str
    record: ExecutionRecord


@dataclass(frozen=True, slots=True)
class ActionRecord:
    action_id: str
    intent_id: str
    intent_version: int
    module_id: str
    module_version: str
    arguments: dict[str, Any]
    principal: PrincipalIdentity
    delivery_target: DeliveryTarget
    requested_by: PrincipalIdentity
    confirmed_by: PrincipalIdentity | None
    risk_level: RiskLevel
    permissions: frozenset[str]
    payload_hash: str
    idempotency_key: str
    status: ActionStatus
    result: IntentResult | None
    expires_at: datetime
    created_at: datetime
    updated_at: datetime


class ExecutionRepository(Protocol):
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
    ) -> ExecutionReservation: ...

    async def finish(
        self,
        execution_id: str,
        *,
        status: ExecutionStatus,
        result: IntentResult,
        provider_request_id: str = "",
    ) -> ExecutionRecord: ...

    async def get(self, execution_id: str) -> ExecutionRecord | None: ...

    async def retry_unknown(self, execution_id: str) -> ExecutionRecord | None: ...

    async def resolve_unknown(
        self,
        execution_id: str,
        *,
        status: ExecutionStatus,
        result: IntentResult,
    ) -> ExecutionRecord | None: ...


class ActionRepository(Protocol):
    async def create(self, action: ActionRecord) -> ActionRecord: ...

    async def get(self, action_id: str) -> ActionRecord | None: ...

    async def claim(
        self,
        action_id: str,
        *,
        confirmed_by: PrincipalIdentity,
        now: datetime,
    ) -> ActionRecord | None: ...

    async def finish(
        self,
        action_id: str,
        *,
        status: ActionStatus,
        result: IntentResult,
    ) -> ActionRecord: ...

    async def cancel(
        self,
        action_id: str,
        *,
        principal: PrincipalIdentity,
        now: datetime,
    ) -> ActionRecord | None: ...

    async def claim_unknown_for_retry(
        self,
        action_id: str,
        *,
        principal: PrincipalIdentity,
    ) -> ActionRecord | None: ...

    async def resolve_unknown(
        self,
        action_id: str,
        *,
        status: ActionStatus,
        result: IntentResult,
    ) -> ActionRecord | None: ...


class AuditRepository(Protocol):
    async def append(
        self,
        *,
        trace_id: str,
        request_id: str,
        tenant_id: str,
        principal_id: str,
        channel: str,
        intent_id: str,
        intent_version: int,
        module_id: str,
        phase: str,
        outcome: str,
        payload_hash: str,
        metadata: dict[str, Any],
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class ConversationMessage:
    message_id: str
    conversation_key: str
    role: str
    content: str
    created_at: datetime


class ConversationRepository(Protocol):
    async def append_message(
        self,
        *,
        conversation: ConversationRef,
        message_id: str,
        role: str,
        content: str,
    ) -> bool: ...

    async def recent_messages(
        self,
        conversation: ConversationRef,
        *,
        limit: int,
    ) -> tuple[ConversationMessage, ...]: ...

    async def get_summary(self, conversation: ConversationRef) -> str: ...

    async def save_summary(self, conversation: ConversationRef, summary: str) -> None: ...


class MemoryRepository(Protocol):
    async def list_memories(
        self,
        principal: PrincipalIdentity,
        *,
        limit: int = 20,
    ) -> dict[str, str]: ...

    async def put_memory(
        self,
        principal: PrincipalIdentity,
        *,
        key: str,
        value: str,
    ) -> None: ...


class AttachmentRepository(Protocol):
    async def create(
        self,
        *,
        attachment: AttachmentRef,
        principal: PrincipalIdentity,
        conversation: ConversationRef | None,
        storage_key: str,
        temporary: bool,
    ) -> None: ...

    async def get_owned(
        self,
        attachment_id: str,
        *,
        principal: PrincipalIdentity,
        conversation: ConversationRef | None,
    ) -> tuple[AttachmentRef, str] | None: ...


class DeliveryBindingRepository(Protocol):
    async def resolve(
        self,
        binding_id: str,
        *,
        principal: PrincipalIdentity,
    ) -> DeliveryTarget | None: ...

    async def save(
        self,
        *,
        binding_id: str,
        principal: PrincipalIdentity,
        target: DeliveryTarget,
    ) -> None: ...


class ModuleRecordRepository(Protocol):
    async def get(
        self,
        *,
        module_id: str,
        record_type: str,
        record_id: str,
        principal: PrincipalIdentity,
    ) -> dict[str, Any] | None: ...

    async def put(
        self,
        *,
        module_id: str,
        record_type: str,
        record_id: str,
        principal: PrincipalIdentity,
        payload: dict[str, Any],
    ) -> None: ...

    async def delete(
        self,
        *,
        module_id: str,
        record_type: str,
        record_id: str,
        principal: PrincipalIdentity,
    ) -> bool: ...


class EventDedupRepository(Protocol):
    async def claim(self, event_id: str, *, source: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class SchedulerLeaseClaim:
    job_id: str
    owner_id: str
    fencing_token: int
    lease_until: datetime


class SchedulerLeaseRepository(Protocol):
    async def claim(
        self,
        job_id: str,
        *,
        owner_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> SchedulerLeaseClaim | None: ...
