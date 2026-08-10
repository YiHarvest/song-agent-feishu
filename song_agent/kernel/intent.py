"""Intent metadata and handler contracts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel

from .models import ExecutionContext, IntentResult


class OperationKind(StrEnum):
    READ = "read"
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    OVERWRITE = "overwrite"
    BULK = "bulk"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class IdempotencyPolicy(StrEnum):
    NONE = "none"
    REQUIRED = "required"


class UnknownResolutionPolicy(StrEnum):
    NONE = "none"
    RECONCILE = "reconcile"
    RETRY_IDEMPOTENT = "retry_idempotent"
    RECONCILE_THEN_RETRY = "reconcile_then_retry"


@dataclass(frozen=True, slots=True)
class ConfirmationPolicy:
    required: bool = False
    ttl_seconds: int = 1800


@dataclass(frozen=True, slots=True)
class ContextRequirement:
    provider_id: str
    required: bool = True
    max_chars: int = 4000


class IntentHandler(Protocol):
    async def __call__(
        self,
        context: ExecutionContext,
        arguments: BaseModel,
    ) -> IntentResult: ...


PermissionResolver = Callable[[BaseModel], frozenset[str]]
ReconcileHandler = Callable[[ExecutionContext, BaseModel], Awaitable[IntentResult | None]]


@dataclass(frozen=True, slots=True)
class IntentSpec:
    intent_id: str
    version: int
    module_id: str
    description: str
    arguments_model: type[BaseModel]
    handler: IntentHandler
    result_model: type[BaseModel] | None = None
    top_level_exposed: bool = True
    agent_exposed: bool = False
    operation_kind: OperationKind = OperationKind.READ
    risk_level: RiskLevel = RiskLevel.LOW
    required_permissions: frozenset[str] = frozenset()
    permission_resolver: PermissionResolver | None = None
    context_requirements: tuple[ContextRequirement, ...] = ()
    idempotency_policy: IdempotencyPolicy = IdempotencyPolicy.NONE
    confirmation_policy: ConfirmationPolicy = field(default_factory=ConfirmationPolicy)
    unknown_resolution_policy: UnknownResolutionPolicy = UnknownResolutionPolicy.NONE
    reconcile: ReconcileHandler | None = None
    execution_timeout_seconds: float = 30.0
    examples: tuple[str, ...] = ()

    @property
    def is_write(self) -> bool:
        return self.operation_kind is not OperationKind.READ
