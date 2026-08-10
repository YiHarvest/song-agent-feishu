from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict
from test_dispatcher_actions import envelope

from song_agent.adapters.auth import SemanticAuthorization
from song_agent.infrastructure.database import Database
from song_agent.infrastructure.repositories import (
    SqliteActionRepository,
    SqliteAuditRepository,
    SqliteExecutionRepository,
)
from song_agent.kernel.errors import DeliveryState, ProviderError
from song_agent.kernel.intent import (
    ConfirmationPolicy,
    IdempotencyPolicy,
    IntentSpec,
    OperationKind,
    RiskLevel,
    UnknownResolutionPolicy,
)
from song_agent.kernel.models import ExecutionContext, IntentResult
from song_agent.ports.repositories import ActionStatus, ExecutionStatus
from song_agent.runtime.actions import ActionService
from song_agent.runtime.catalog import IntentCatalog
from song_agent.runtime.context import ContextAssembler
from song_agent.runtime.dispatcher import IntentDispatcher
from song_agent.runtime.risk import RiskPolicy


class WriteArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str


class UncertainThenSuccessfulHandler:
    def __init__(self) -> None:
        self.keys: list[str] = []
        self.reconcile_calls = 0

    async def __call__(
        self, context: ExecutionContext, arguments: BaseModel
    ) -> IntentResult:
        self.keys.append(context.idempotency_key)
        if len(self.keys) == 1:
            raise ProviderError(
                "provider.timeout",
                "result unknown",
                delivery_state=DeliveryState.ACCEPTED,
            )
        return IntentResult.success(code="danger.write.succeeded")

    async def reconcile(
        self, context: ExecutionContext, arguments: BaseModel
    ) -> IntentResult | None:
        self.reconcile_calls += 1
        raise ProviderError(
            "provider.reconcile_unavailable",
            "cannot verify remote state",
            delivery_state=DeliveryState.NOT_SENT,
        )


@pytest.mark.asyncio
async def test_unknown_can_only_reconcile_then_retry_through_action_service(database_path) -> None:
    database = Database(database_path)
    executions = SqliteExecutionRepository(database)
    actions = SqliteActionRepository(database)
    catalog = IntentCatalog()
    catalog.declare_module("danger", frozenset({"danger"}))
    handler = UncertainThenSuccessfulHandler()
    catalog.register(
        IntentSpec(
            intent_id="danger.write",
            version=1,
            module_id="danger",
            description="test uncertain write",
            arguments_model=WriteArguments,
            result_model=IntentResult,
            handler=handler,
            operation_kind=OperationKind.DELETE,
            risk_level=RiskLevel.HIGH,
            idempotency_policy=IdempotencyPolicy.REQUIRED,
            confirmation_policy=ConfirmationPolicy(required=True),
            unknown_resolution_policy=UnknownResolutionPolicy.RECONCILE_THEN_RETRY,
            reconcile=handler.reconcile,
        )
    )
    dispatcher = IntentDispatcher(
        catalog=catalog,
        authorization=SemanticAuthorization(),
        executions=executions,
        actions=actions,
        audit=SqliteAuditRepository(database),
        contexts=ContextAssembler(),
        risk_policy=RiskPolicy(),
        module_versions={"danger": "1.0.0"},
    )
    service = ActionService(actions, dispatcher, catalog, executions)
    principal = envelope("plan.get", {}).principal
    pending = await dispatcher.execute(envelope("danger.write", {"value": "x"}))
    action_id = pending.data["action_id"]

    first = await service.confirm(action_id, principal)
    assert first.code == "provider.timeout"
    unknown = await service.get(action_id, principal)
    assert unknown is not None and unknown.status is ActionStatus.UNKNOWN
    execution = await executions.get(f"exec_{action_id}")
    assert execution is not None and execution.status is ExecutionStatus.UNKNOWN
    assert not hasattr(dispatcher, "execute_confirmed")

    checked = await service.reconcile(action_id, principal)
    assert checked.code == "action.still_unknown"
    still_unknown = await service.get(action_id, principal)
    assert still_unknown is not None and still_unknown.status is ActionStatus.UNKNOWN

    retried = await service.retry_unknown(action_id, principal)
    assert retried.code == "danger.write.succeeded"
    resolved = await service.get(action_id, principal)
    assert resolved is not None and resolved.status is ActionStatus.SUCCEEDED
    assert handler.keys == ["idem-req-1", "idem-req-1"]
    assert handler.reconcile_calls == 2
