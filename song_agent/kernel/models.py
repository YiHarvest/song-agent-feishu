"""Channel-neutral request, identity, attachment, and result models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .ids import validate_intent_id


class PrincipalIdentity(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=255)
    principal_id: str = Field(min_length=1, max_length=255)
    auth_provider: str = Field(min_length=1, max_length=80)
    attributes: dict[str, str] = Field(default_factory=dict)


class DeliveryTarget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    channel: str = Field(min_length=1, max_length=80)
    channel_account_id: str = Field(min_length=1, max_length=255)
    destination_id: str = Field(min_length=1, max_length=255)
    thread_id: str = Field(default="", max_length=255)


class ConversationRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=255)
    channel: str = Field(min_length=1, max_length=80)
    channel_account_id: str = Field(min_length=1, max_length=255)
    conversation_id: str = Field(min_length=1, max_length=255)
    principal_id: str = Field(min_length=1, max_length=255)
    thread_id: str = Field(default="", max_length=255)

    @property
    def key(self) -> str:
        return ":".join(
            (
                self.tenant_id,
                self.channel,
                self.channel_account_id,
                self.conversation_id,
                self.thread_id or "_",
                self.principal_id,
            )
        )


class AttachmentRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    attachment_id: str = Field(min_length=1, max_length=255)
    media_type: str = Field(min_length=1, max_length=255)
    filename: str = Field(default="", max_length=1024)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    expires_at: datetime | None = None


class TextPart(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["text"] = "text"
    text: str = Field(min_length=1, max_length=100_000)


class AttachmentPart(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["attachment"] = "attachment"
    attachment: AttachmentRef


ContentPart = Annotated[TextPart | AttachmentPart, Field(discriminator="kind")]


class NaturalLanguagePayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["natural_language"] = "natural_language"
    content: tuple[ContentPart, ...] = Field(min_length=1)

    @property
    def text(self) -> str:
        return "\n".join(part.text for part in self.content if isinstance(part, TextPart))


class IntentExecutionPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["intent_execution"] = "intent_execution"
    intent_id: str
    arguments: dict[str, Any] = Field(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        object.__setattr__(self, "intent_id", validate_intent_id(self.intent_id))


RequestPayload = Annotated[
    NaturalLanguagePayload | IntentExecutionPayload,
    Field(discriminator="kind"),
]


class RequestEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str = Field(min_length=1, max_length=255)
    principal: PrincipalIdentity
    delivery_target: DeliveryTarget
    conversation: ConversationRef | None = None
    source: Literal["feishu", "openai_api", "intent_api", "scheduler", "agent", "action"]
    payload: RequestPayload
    metadata: dict[str, Any] = Field(default_factory=dict)
    parent_request_id: str = ""
    agent_run_id: str = ""


class PresentationHints(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["plain", "success", "warning", "confirmation", "list"] = "plain"
    title: str = ""


class IntentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal[
        "success",
        "clarification_required",
        "authorization_required",
        "confirmation_required",
        "in_progress",
        "failure",
    ]
    code: str = Field(min_length=1, max_length=160)
    data: dict[str, Any] = Field(default_factory=dict)
    message: str = ""
    presentation_hints: PresentationHints = Field(default_factory=PresentationHints)

    @classmethod
    def success(
        cls,
        *,
        code: str,
        data: dict[str, Any] | None = None,
        message: str = "",
        kind: Literal["plain", "success", "warning", "confirmation", "list"] = "success",
    ) -> IntentResult:
        return cls(
            status="success",
            code=code,
            data=data or {},
            message=message,
            presentation_hints=PresentationHints(kind=kind),
        )

    @classmethod
    def failure(cls, *, code: str, message: str, data: dict[str, Any] | None = None) -> IntentResult:
        return cls(status="failure", code=code, message=message, data=data or {})


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    execution_id: str
    envelope: RequestEnvelope
    intent_id: str
    intent_version: int
    idempotency_key: str
    deadline: float
    contexts: dict[str, Any] = field(default_factory=dict)
    permissions: frozenset[str] = frozenset()
