from __future__ import annotations

from dataclasses import dataclass

from pydantic import ValidationError

from ..kernel.models import (
    IntentExecutionPayload,
    IntentResult,
    NaturalLanguagePayload,
    RequestEnvelope,
)
from ..ports.llm import IntentModelPort
from .catalog import IntentCatalog
from .context import ContextAssembler


@dataclass(frozen=True, slots=True)
class RouteOutcome:
    envelope: RequestEnvelope | None = None
    result: IntentResult | None = None


class TopLevelRouter:
    def __init__(
        self,
        catalog: IntentCatalog,
        model: IntentModelPort,
        contexts: ContextAssembler,
        *,
        minimum_confidence: float = 0.65,
    ) -> None:
        self.catalog = catalog
        self.model = model
        self.contexts = contexts
        self.minimum_confidence = minimum_confidence

    async def route(self, envelope: RequestEnvelope) -> RouteOutcome:
        if not isinstance(envelope.payload, NaturalLanguagePayload):
            return RouteOutcome(
                result=IntentResult.failure(
                    code="router.invalid_payload",
                    message="TopLevelRouter only accepts natural-language payloads.",
                )
            )
        context = await self.contexts.for_router(envelope)
        descriptors = self.catalog.top_level_descriptors()
        repair_instruction = ""
        for attempt in range(2):
            decision = await self.model.route(
                envelope.payload.content,
                descriptors,
                context,
                repair_instruction=repair_instruction,
            )
            if decision.missing_fields:
                return RouteOutcome(
                    result=IntentResult(
                        status="clarification_required",
                        code="router.missing_fields",
                        data={"missing_fields": decision.missing_fields},
                        message="还需要：" + "、".join(decision.missing_fields),
                    )
                )
            if decision.confidence < self.minimum_confidence:
                return RouteOutcome(
                    result=IntentResult(
                        status="clarification_required",
                        code="router.low_confidence",
                        message="请求意图不够明确，请补充要执行的操作和对象。",
                    )
                )
            try:
                spec = self.catalog.resolve(decision.intent_id)
                if not spec.top_level_exposed:
                    raise ValueError("intent is not exposed to the top-level router")
                spec.arguments_model.model_validate(decision.arguments)
            except (ValueError, ValidationError) as error:
                repair_instruction = f"Previous routing output was invalid: {error}"
                if attempt == 0:
                    continue
                return RouteOutcome(
                    result=IntentResult(
                        status="clarification_required",
                        code="router.invalid_decision",
                        message="无法可靠解析请求，请补充更明确的操作参数。",
                    )
                )
            return RouteOutcome(
                envelope=envelope.model_copy(
                    update={
                        "payload": IntentExecutionPayload(
                            intent_id=spec.intent_id,
                            arguments=decision.arguments,
                        )
                    }
                )
            )
        raise AssertionError("router attempt loop must return")
