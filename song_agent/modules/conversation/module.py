from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ...kernel.intent import ContextRequirement, IntentSpec
from ...kernel.models import ExecutionContext, IntentResult, RequestEnvelope
from ...kernel.module import ContributionSink, ModuleManifest
from ...ports.llm import ConversationModelPort
from ...ports.repositories import ConversationRepository


class ConversationRespondArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=100_000)


class RecentConversationContext:
    def __init__(self, repository: ConversationRepository, *, limit: int = 20) -> None:
        self.repository = repository
        self.limit = limit

    async def provide(self, envelope: RequestEnvelope, *, max_chars: int) -> list[dict[str, str]]:
        if envelope.conversation is None:
            return []
        messages = await self.repository.recent_messages(envelope.conversation, limit=self.limit)
        remaining = max_chars
        result: list[dict[str, str]] = []
        for message in reversed(messages):
            content = message.content[:remaining]
            if not content:
                break
            result.append({"role": message.role, "content": content})
            remaining -= len(content)
        return list(reversed(result))


class ConversationSummaryContext:
    def __init__(self, repository: ConversationRepository) -> None:
        self.repository = repository

    async def provide(self, envelope: RequestEnvelope, *, max_chars: int) -> str:
        if envelope.conversation is None:
            return ""
        return (await self.repository.get_summary(envelope.conversation))[:max_chars]


class ConversationRespondHandler:
    def __init__(
        self,
        model: ConversationModelPort,
        repository: ConversationRepository,
    ) -> None:
        self.model = model
        self.repository = repository

    async def __call__(
        self,
        context: ExecutionContext,
        arguments: BaseModel,
    ) -> IntentResult:
        args = ConversationRespondArguments.model_validate(arguments)
        answer = await self.model.respond(args.text, context.contexts)
        conversation = context.envelope.conversation
        if conversation is not None:
            await self.repository.append_message(
                conversation=conversation,
                message_id=context.envelope.request_id,
                role="user",
                content=args.text,
            )
            await self.repository.append_message(
                conversation=conversation,
                message_id=f"{context.execution_id}:assistant",
                role="assistant",
                content=answer,
            )
        return IntentResult.success(
            code="conversation.responded",
            data={"text": answer},
            message=answer,
            kind="plain",
        )


class ConversationModule:
    manifest = ModuleManifest(
        module_id="conversation",
        version="1.0.0",
        owns_namespaces=frozenset({"conversation"}),
    )

    def __init__(
        self,
        *,
        model: ConversationModelPort,
        repository: ConversationRepository,
    ) -> None:
        self.model = model
        self.repository = repository

    def contribute(self, sink: ContributionSink) -> None:
        sink.add_context_provider(
            "conversation.recent", RecentConversationContext(self.repository)
        )
        sink.add_context_provider(
            "conversation.summary", ConversationSummaryContext(self.repository)
        )
        sink.add_intent(
            IntentSpec(
                intent_id="conversation.respond",
                version=1,
                module_id="conversation",
                description="Respond to ordinary conversation or a single general question.",
                arguments_model=ConversationRespondArguments,
                result_model=IntentResult,
                handler=ConversationRespondHandler(self.model, self.repository),
                context_requirements=(
                    ContextRequirement("conversation.recent", required=False, max_chars=6000),
                    ContextRequirement("conversation.summary", required=False, max_chars=3000),
                ),
                examples=("你好", "解释一下这个概念"),
            )
        )
