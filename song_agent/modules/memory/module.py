from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ...kernel.intent import IdempotencyPolicy, IntentSpec, OperationKind, RiskLevel
from ...kernel.models import ExecutionContext, IntentResult, RequestEnvelope
from ...kernel.module import ContributionSink, ModuleManifest
from ...ports.repositories import MemoryRepository


class RememberArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=255)
    value: str = Field(min_length=1, max_length=20_000)


class ListMemoryArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=20, ge=1, le=100)


class MemoryContext:
    def __init__(self, repository: MemoryRepository) -> None:
        self.repository = repository

    async def provide(self, envelope: RequestEnvelope, *, max_chars: int) -> dict[str, str]:
        memories = await self.repository.list_memories(envelope.principal)
        remaining = max_chars
        output: dict[str, str] = {}
        for key, value in memories.items():
            clipped = value[:remaining]
            if not clipped:
                break
            output[key] = clipped
            remaining -= len(key) + len(clipped)
        return output


class RememberHandler:
    def __init__(self, repository: MemoryRepository) -> None:
        self.repository = repository

    async def __call__(self, context: ExecutionContext, arguments: BaseModel) -> IntentResult:
        args = RememberArguments.model_validate(arguments)
        await self.repository.put_memory(
            context.envelope.principal,
            key=args.key,
            value=args.value,
        )
        return IntentResult.success(
            code="memory.saved", data={"key": args.key}, message="已保存长期记忆。"
        )


class ListMemoryHandler:
    def __init__(self, repository: MemoryRepository) -> None:
        self.repository = repository

    async def __call__(self, context: ExecutionContext, arguments: BaseModel) -> IntentResult:
        args = ListMemoryArguments.model_validate(arguments)
        memories = await self.repository.list_memories(
            context.envelope.principal, limit=args.limit
        )
        return IntentResult.success(
            code="memory.listed", data={"memories": memories}, kind="list"
        )


class MemoryModule:
    manifest = ModuleManifest(
        module_id="memory",
        version="1.0.0",
        owns_namespaces=frozenset({"memory"}),
    )

    def __init__(self, repository: MemoryRepository) -> None:
        self.repository = repository

    def contribute(self, sink: ContributionSink) -> None:
        sink.add_context_provider("memory.long_term", MemoryContext(self.repository))
        sink.add_intent(
            IntentSpec(
                intent_id="memory.remember",
                version=1,
                module_id="memory",
                description="Save one fact as cross-conversation long-term memory.",
                arguments_model=RememberArguments,
                result_model=IntentResult,
                handler=RememberHandler(self.repository),
                agent_exposed=True,
                operation_kind=OperationKind.UPDATE,
                risk_level=RiskLevel.MEDIUM,
                idempotency_policy=IdempotencyPolicy.REQUIRED,
            )
        )
        sink.add_intent(
            IntentSpec(
                intent_id="memory.list",
                version=1,
                module_id="memory",
                description="List the current principal's long-term memories.",
                arguments_model=ListMemoryArguments,
                result_model=IntentResult,
                handler=ListMemoryHandler(self.repository),
                agent_exposed=True,
            )
        )
