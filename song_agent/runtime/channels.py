from __future__ import annotations

from ..kernel.errors import DeliveryState, ProviderError
from ..kernel.models import DeliveryTarget, IntentResult, RequestEnvelope
from ..ports.channel import ChannelPresenter


class ChannelRegistry:
    def __init__(self) -> None:
        self._presenters: dict[str, ChannelPresenter] = {}

    def register(self, presenter: ChannelPresenter) -> None:
        if presenter.channel_id in self._presenters:
            raise ValueError(f"channel presenter already registered: {presenter.channel_id}")
        self._presenters[presenter.channel_id] = presenter

    async def deliver_many(
        self,
        targets: tuple[DeliveryTarget, ...],
        result: IntentResult,
        *,
        request: RequestEnvelope,
    ) -> list[dict[str, str]]:
        delivered: list[dict[str, str]] = []
        for index, target in enumerate(targets):
            presenter = self._presenters.get(target.channel)
            if presenter is None:
                if delivered:
                    raise ProviderError(
                        "delivery.partial_unknown",
                        "Some targets were delivered before an unavailable channel was found.",
                        delivery_state=DeliveryState.UNKNOWN,
                    )
                raise ProviderError(
                    "delivery.channel_unavailable",
                    f"No presenter is registered for channel {target.channel!r}.",
                    delivery_state=DeliveryState.NOT_SENT,
                )
            child = request.model_copy(update={"request_id": f"{request.request_id}:{index}"})
            try:
                delivery_id = await presenter.present(target, result, request=child)
            except ProviderError as error:
                if delivered and error.delivery_state in {
                    DeliveryState.NOT_SENT,
                    DeliveryState.REJECTED,
                }:
                    raise ProviderError(
                        "delivery.partial_unknown",
                        "Only part of the broadcast could be verified.",
                        delivery_state=DeliveryState.UNKNOWN,
                    ) from error
                raise
            delivered.append(
                {"channel": target.channel, "delivery_id": delivery_id or ""}
            )
        return delivered
