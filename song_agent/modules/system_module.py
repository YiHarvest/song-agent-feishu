from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ..kernel.intent import (
    ConfirmationPolicy,
    IdempotencyPolicy,
    IntentSpec,
    OperationKind,
    RiskLevel,
    UnknownResolutionPolicy,
)
from ..kernel.models import DeliveryTarget, ExecutionContext, IntentResult
from ..kernel.module import ContributionSink, ModuleManifest
from ..runtime.channels import ChannelRegistry


class SchedulerBroadcastArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    targets: tuple[DeliveryTarget, ...] = Field(min_length=1, max_length=500)
    message: str = Field(min_length=1, max_length=100_000)


class SchedulerBroadcastHandler:
    def __init__(self, channels: ChannelRegistry) -> None:
        self.channels = channels

    async def __call__(self, context: ExecutionContext, arguments: BaseModel) -> IntentResult:
        args = SchedulerBroadcastArguments.model_validate(arguments)
        delivery_result = IntentResult.success(
            code="system.scheduler.message",
            data={"message": args.message},
            message=args.message,
            kind="plain",
        )
        deliveries = await self.channels.deliver_many(
            args.targets,
            delivery_result,
            request=context.envelope,
        )
        return IntentResult.success(
            code="system.scheduler.broadcast_succeeded",
            data={"deliveries": deliveries},
        )


class SystemModule:
    manifest = ModuleManifest(
        module_id="system",
        version="1.0.0",
        owns_namespaces=frozenset({"system"}),
    )

    def __init__(self, channels: ChannelRegistry) -> None:
        self.channels = channels

    def contribute(self, sink: ContributionSink) -> None:
        sink.add_intent(
            IntentSpec(
                intent_id="system.scheduler.broadcast",
                version=1,
                module_id="system",
                description="Broadcast a scheduled message through registered channel presenters.",
                arguments_model=SchedulerBroadcastArguments,
                result_model=IntentResult,
                handler=SchedulerBroadcastHandler(self.channels),
                top_level_exposed=False,
                agent_exposed=False,
                operation_kind=OperationKind.BULK,
                risk_level=RiskLevel.HIGH,
                idempotency_policy=IdempotencyPolicy.REQUIRED,
                confirmation_policy=ConfirmationPolicy(required=True),
                unknown_resolution_policy=UnknownResolutionPolicy.RETRY_IDEMPOTENT,
                execution_timeout_seconds=180,
            )
        )
