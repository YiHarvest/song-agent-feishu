from __future__ import annotations

from typing import Any

from ..kernel.errors import DeliveryState, ProviderError
from ..kernel.models import PrincipalIdentity
from ..ports.search import SearchHit


class UnavailableWorkspaceProvider:
    def __init__(self, provider_id: str) -> None:
        self.provider_id = provider_id

    async def create(
        self,
        principal: PrincipalIdentity,
        arguments: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> Any:
        self._raise()

    async def query(
        self, principal: PrincipalIdentity, arguments: dict[str, Any]
    ) -> Any:
        self._raise()

    async def update(
        self,
        principal: PrincipalIdentity,
        arguments: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> Any:
        self._raise()

    async def delete(
        self,
        principal: PrincipalIdentity,
        arguments: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> Any:
        self._raise()

    async def reconcile(
        self,
        principal: PrincipalIdentity,
        arguments: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> None:
        return None

    async def execute(
        self,
        operation: str,
        principal: PrincipalIdentity,
        arguments: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> Any:
        self._raise()

    def _raise(self) -> None:
        raise ProviderError(
            "provider.not_configured",
            f"Provider {self.provider_id!r} is not configured.",
            delivery_state=DeliveryState.NOT_SENT,
        )


class UnavailableSearchProvider:
    async def search(
        self,
        query: str,
        *,
        provider: str = "auto",
        max_results: int = 5,
    ) -> tuple[SearchHit, ...]:
        raise ProviderError(
            "search.provider_not_configured",
            "Search provider is not configured.",
            delivery_state=DeliveryState.NOT_SENT,
        )


class UnavailableAttachmentUnderstandingProvider:
    async def analyze(self, path: Any, media_type: str, instruction: str) -> dict[str, Any]:
        self._raise()

    async def transcribe(
        self,
        path: Any,
        *,
        filename: str,
        media_type: str,
        language: str,
    ) -> dict[str, Any]:
        self._raise()

    async def parse(
        self,
        path: Any,
        *,
        filename: str,
        media_type: str,
        instruction: str,
    ) -> dict[str, Any]:
        self._raise()

    @staticmethod
    def _raise() -> None:
        raise ProviderError(
            "attachment.understanding_provider_not_configured",
            "Attachment understanding provider is not configured.",
            delivery_state=DeliveryState.NOT_SENT,
        )
