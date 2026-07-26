"""统一意图与请求模型。"""

from __future__ import annotations

from typing import Any, Literal, get_args

from pydantic import BaseModel, Field, model_validator

from ..models import FeishuIdentity

IntentName = Literal[
    "calendar.create",
    "calendar.query",
    "calendar.update",
    "calendar.delete",
    "task.create",
    "task.query",
    "task.update",
    "task.complete",
    "task.delete",
    "reminder.create",
    "reminder.query",
    "reminder.cancel",
    "pending_action.confirm",
    "pending_action.cancel",
    "pending_action.retry",
    "user.authorization.status",
    "user.preferences.get",
    "user.preferences.update",
    "conversation.general",
    "content.summarize",
    "content.analyze",
    "recording.analyze",
    "workspace.explore",
    "multi_source.research",
]
INTENT_NAMES = frozenset(get_args(IntentName))

DETERMINISTIC_INTENTS: frozenset[str] = frozenset(
    {
        "calendar.create",
        "calendar.query",
        "calendar.update",
        "calendar.delete",
        "task.create",
        "task.query",
        "task.update",
        "task.complete",
        "task.delete",
        "reminder.create",
        "reminder.query",
        "reminder.cancel",
        "pending_action.confirm",
        "pending_action.cancel",
        "pending_action.retry",
        "user.authorization.status",
        "user.preferences.get",
        "user.preferences.update",
    }
)


class ExtractedIntent(BaseModel):
    intent: IntentName
    arguments: dict[str, Any] = Field(default_factory=dict)
    missing_fields: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="before")
    @classmethod
    def normalize_model_output(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        raw_intent = normalized.get("intent")
        if raw_intent is None:
            raw_intent = normalized.get("intent_type")
        if isinstance(raw_intent, str):
            normalized["intent"] = (
                raw_intent if raw_intent in INTENT_NAMES else "conversation.general"
            )
        return normalized


class UserRequest(BaseModel):
    identity: FeishuIdentity
    text: str
    source: Literal["feishu", "react", "api"] = "feishu"
    chat_id: str = ""
    thread_id: str = ""
    message_id: str = ""
    event_id: str = ""
    context: dict[str, Any] = Field(default_factory=dict)
