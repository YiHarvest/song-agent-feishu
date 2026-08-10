"""Stable error contracts crossing provider and execution boundaries."""

from __future__ import annotations

from enum import StrEnum


class DeliveryState(StrEnum):
    NOT_SENT = "not_sent"
    REJECTED = "rejected"
    ACCEPTED = "accepted"
    UNKNOWN = "unknown"


class ProviderError(RuntimeError):
    def __init__(
        self,
        error_code: str,
        safe_message: str,
        *,
        retryable: bool = False,
        delivery_state: DeliveryState = DeliveryState.NOT_SENT,
        provider_request_id: str = "",
    ) -> None:
        super().__init__(safe_message)
        self.error_code = error_code
        self.safe_message = safe_message
        self.retryable = retryable
        self.delivery_state = delivery_state
        self.provider_request_id = provider_request_id


class CatalogError(RuntimeError):
    """Raised when module contributions cannot form a valid catalog."""


class DispatchError(RuntimeError):
    """Internal execution-pipeline error."""
