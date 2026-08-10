from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ...kernel.intent import (
    ConfirmationPolicy,
    IdempotencyPolicy,
    IntentSpec,
    OperationKind,
    RiskLevel,
)
from ...kernel.models import ExecutionContext, IntentResult
from ...kernel.module import ContributionSink, ModuleManifest
from ...ports.repositories import ModuleRecordRepository


class PlanGetArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: str = Field(min_length=1, max_length=255)


class PlanSaveArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: str = Field(min_length=1, max_length=255)
    plan: dict[str, Any]


class PlanHandler:
    def __init__(self, repository: ModuleRecordRepository, operation: str) -> None:
        self.repository = repository
        self.operation = operation

    async def __call__(self, context: ExecutionContext, arguments: BaseModel) -> IntentResult:
        principal = context.envelope.principal
        plan_id = str(arguments.plan_id)
        if self.operation == "get":
            plan = await self.repository.get(
                module_id="plan",
                record_type="plan",
                record_id=plan_id,
                principal=principal,
            )
            if plan is None:
                return IntentResult.failure(code="plan.not_found", message="计划不存在。")
            return IntentResult.success(code="plan.loaded", data={"plan_id": plan_id, "plan": plan})
        if self.operation == "save":
            await self.repository.put(
                module_id="plan",
                record_type="plan",
                record_id=plan_id,
                principal=principal,
                payload=arguments.plan,
            )
            return IntentResult.success(code="plan.saved", data={"plan_id": plan_id})
        deleted = await self.repository.delete(
            module_id="plan",
            record_type="plan",
            record_id=plan_id,
            principal=principal,
        )
        return IntentResult.success(
            code="plan.deleted", data={"plan_id": plan_id, "deleted": deleted}
        )


class PlanModule:
    manifest = ModuleManifest(
        module_id="plan", version="1.0.0", owns_namespaces=frozenset({"plan"})
    )

    def __init__(self, repository: ModuleRecordRepository) -> None:
        self.repository = repository

    def contribute(self, sink: ContributionSink) -> None:
        sink.add_intent(
            IntentSpec(
                intent_id="plan.get",
                version=1,
                module_id="plan",
                description="Load a saved structured plan.",
                arguments_model=PlanGetArguments,
                result_model=IntentResult,
                handler=PlanHandler(self.repository, "get"),
                agent_exposed=True,
            )
        )
        sink.add_intent(
            IntentSpec(
                intent_id="plan.save",
                version=1,
                module_id="plan",
                description="Create or update a structured plan.",
                arguments_model=PlanSaveArguments,
                result_model=IntentResult,
                handler=PlanHandler(self.repository, "save"),
                agent_exposed=True,
                operation_kind=OperationKind.UPDATE,
                risk_level=RiskLevel.MEDIUM,
                idempotency_policy=IdempotencyPolicy.REQUIRED,
            )
        )
        sink.add_intent(
            IntentSpec(
                intent_id="plan.delete",
                version=1,
                module_id="plan",
                description="Delete a saved plan.",
                arguments_model=PlanGetArguments,
                result_model=IntentResult,
                handler=PlanHandler(self.repository, "delete"),
                agent_exposed=True,
                operation_kind=OperationKind.DELETE,
                risk_level=RiskLevel.HIGH,
                idempotency_policy=IdempotencyPolicy.REQUIRED,
                confirmation_policy=ConfirmationPolicy(required=True),
            )
        )
