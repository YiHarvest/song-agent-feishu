"""Public, infrastructure-free contracts for Song Agent modules."""

from .errors import DeliveryState, ProviderError
from .intent import (
    ConfirmationPolicy,
    IdempotencyPolicy,
    IntentHandler,
    IntentSpec,
    OperationKind,
    RiskLevel,
    UnknownResolutionPolicy,
)
from .models import (
    AttachmentPart,
    AttachmentRef,
    ConversationRef,
    DeliveryTarget,
    IntentExecutionPayload,
    IntentResult,
    NaturalLanguagePayload,
    PrincipalIdentity,
    RequestEnvelope,
    TextPart,
)
from .module import Module, ModuleManifest

__all__ = [
    "AttachmentPart",
    "AttachmentRef",
    "ConfirmationPolicy",
    "ConversationRef",
    "DeliveryState",
    "DeliveryTarget",
    "IdempotencyPolicy",
    "IntentExecutionPayload",
    "IntentHandler",
    "IntentResult",
    "IntentSpec",
    "Module",
    "ModuleManifest",
    "NaturalLanguagePayload",
    "OperationKind",
    "PrincipalIdentity",
    "ProviderError",
    "RequestEnvelope",
    "RiskLevel",
    "TextPart",
    "UnknownResolutionPolicy",
]
