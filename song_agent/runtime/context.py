from __future__ import annotations

from typing import Any, Protocol

from ..kernel.intent import ContextRequirement, IntentSpec
from ..kernel.models import RequestEnvelope


class ContextProvider(Protocol):
    async def provide(
        self,
        envelope: RequestEnvelope,
        *,
        max_chars: int,
    ) -> Any: ...


class ContextAssembler:
    def __init__(self) -> None:
        self._providers: dict[str, ContextProvider] = {}

    def register(self, provider_id: str, provider: ContextProvider) -> None:
        if provider_id in self._providers:
            raise ValueError(f"context provider already registered: {provider_id}")
        self._providers[provider_id] = provider

    async def assemble(
        self,
        envelope: RequestEnvelope,
        requirements: tuple[ContextRequirement, ...],
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for requirement in requirements:
            provider = self._providers.get(requirement.provider_id)
            if provider is None:
                if requirement.required:
                    raise RuntimeError(
                        f"required context provider is unavailable: {requirement.provider_id}"
                    )
                continue
            result[requirement.provider_id] = await provider.provide(
                envelope,
                max_chars=requirement.max_chars,
            )
        return result

    async def for_intent(
        self,
        envelope: RequestEnvelope,
        spec: IntentSpec,
    ) -> dict[str, Any]:
        return await self.assemble(envelope, spec.context_requirements)

    async def for_router(self, envelope: RequestEnvelope) -> dict[str, Any]:
        requirements = tuple(
            ContextRequirement(provider_id=provider_id, required=False, max_chars=4000)
            for provider_id in ("conversation.recent", "conversation.summary")
        )
        return await self.assemble(envelope, requirements)
