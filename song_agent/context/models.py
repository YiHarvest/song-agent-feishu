"""上下文各层的结构化模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class RequestContext(BaseModel):
    request_id: str
    tenant_key: str
    app_id: str
    principal_id: str
    channel: Literal["react", "feishu", "api"]
    chat_id: str
    thread_id: str = ""
    message_id: str = ""
    timezone: str
    locale: str = "zh-CN"
    current_time: datetime
    session_id: str


class ConversationMessage(BaseModel):
    message_id: str
    role: Literal["user", "assistant", "tool", "system"]
    content: str
    created_at: int


class MemoryFact(BaseModel):
    memory_type: str
    memory_key: str
    memory_value: str
    confidence: float = Field(ge=0, le=1)
    source_message_id: str = ""
    valid_until: str = ""


class ConversationSummary(BaseModel):
    participants: list[str] = Field(default_factory=list)
    active_topics: list[str] = Field(default_factory=list)
    open_loops: list[dict[str, Any]] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    memory_updates: list[MemoryFact] = Field(default_factory=list)


class BusinessContext(BaseModel):
    request: RequestContext
    recent_messages: list[ConversationMessage] = Field(default_factory=list)
    conversation_summary: ConversationSummary | None = None
    memories: list[MemoryFact] = Field(default_factory=list)
    active_pending_action: dict[str, Any] | None = None
    retrieved: dict[str, Any] = Field(default_factory=dict)


class ContextBudget(BaseModel):
    max_input_tokens: int = 32_000
    reserved_output_tokens: int = 4_000

    @property
    def available_tokens(self) -> int:
        return self.max_input_tokens - self.reserved_output_tokens


class SummaryRecord(BaseModel):
    summary: ConversationSummary
    covered_message_count: int
