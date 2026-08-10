from __future__ import annotations

import hashlib

from pydantic import BaseModel, ConfigDict, Field

from ...kernel.intent import IdempotencyPolicy, IntentSpec, OperationKind
from ...kernel.models import ExecutionContext, IntentResult
from ...kernel.module import ContributionSink, ModuleManifest
from ...ports.repositories import DeliveryBindingRepository


class CreateBindingArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(default="default", min_length=1, max_length=100)


class CreateBindingHandler:
    def __init__(self, repository: DeliveryBindingRepository) -> None:
        self.repository = repository

    async def __call__(self, context: ExecutionContext, arguments: BaseModel) -> IntentResult:
        args = CreateBindingArguments.model_validate(arguments)
        if context.envelope.source != "feishu":
            return IntentResult.failure(
                code="delivery.binding_requires_channel",
                message="投递绑定必须从受信任的飞书会话中创建。",
            )
        target = context.envelope.delivery_target
        material = ":".join(
            (
                context.envelope.principal.tenant_id,
                context.envelope.principal.principal_id,
                target.channel,
                target.channel_account_id,
                target.destination_id,
                args.label,
            )
        )
        binding_id = "binding_" + hashlib.sha256(material.encode()).hexdigest()[:24]
        await self.repository.save(
            binding_id=binding_id,
            principal=context.envelope.principal,
            target=target,
        )
        return IntentResult.success(
            code="delivery.binding_created",
            data={"binding_id": binding_id, "label": args.label},
            message="投递绑定已创建。",
        )


class DeliveryModule:
    manifest = ModuleManifest(
        module_id="delivery",
        version="1.0.0",
        owns_namespaces=frozenset({"delivery"}),
    )

    def __init__(self, repository: DeliveryBindingRepository) -> None:
        self.repository = repository

    def contribute(self, sink: ContributionSink) -> None:
        sink.add_intent(
            IntentSpec(
                intent_id="delivery.create_binding",
                version=1,
                module_id="delivery",
                description="Bind the current trusted Feishu conversation for later API delivery.",
                arguments_model=CreateBindingArguments,
                result_model=IntentResult,
                handler=CreateBindingHandler(self.repository),
                operation_kind=OperationKind.CREATE,
                idempotency_policy=IdempotencyPolicy.REQUIRED,
                examples=("把当前飞书会话绑定给 API",),
            )
        )
