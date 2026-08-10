from __future__ import annotations

from typing import Protocol

from ..kernel.models import DeliveryTarget, IntentResult, RequestEnvelope


class ChannelPresenter(Protocol):
    channel_id: str

    async def present(
        self,
        target: DeliveryTarget,
        result: IntentResult,
        *,
        request: RequestEnvelope,
    ) -> str | None: ...
