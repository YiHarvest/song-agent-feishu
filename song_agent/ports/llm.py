from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from ..kernel.models import ContentPart


class RouteDecision(BaseModel):
    intent_id: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    confidence: float
    missing_fields: list[str] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class IntentDescriptor:
    intent_id: str
    description: str
    arguments_schema: dict[str, Any]
    examples: tuple[str, ...]


class IntentModelPort(Protocol):
    async def route(
        self,
        content: tuple[ContentPart, ...],
        intents: tuple[IntentDescriptor, ...],
        context: dict[str, Any],
        *,
        repair_instruction: str = "",
    ) -> RouteDecision: ...


class ConversationModelPort(Protocol):
    async def respond(
        self,
        text: str,
        context: dict[str, Any],
        *,
        system_instruction: str = "",
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class AgentToolDescriptor:
    intent_id: str
    description: str
    arguments_schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AgentDecision:
    kind: Literal["final_answer", "ask_user", "intent_call"]
    content: str = ""
    intent_id: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)


class AgentModelPort(Protocol):
    async def decide(
        self,
        *,
        user_text: str,
        context: dict[str, Any],
        observations: tuple[dict[str, Any], ...],
        tools: tuple[AgentToolDescriptor, ...],
    ) -> AgentDecision: ...
