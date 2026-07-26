"""上下文各层的结构化模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


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

    @model_validator(mode="before")
    @classmethod
    def normalize_string_fact(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        label, separator, fact_value = value.partition("：")
        if not separator:
            label, separator, fact_value = value.partition(":")
        label = label.strip() or "fact"
        fact_value = fact_value.strip() if separator else value.strip()
        memory_type = "constraint" if "约束" in label else "preference"
        return {
            "memory_type": memory_type,
            "memory_key": label,
            "memory_value": fact_value,
            "confidence": 0.7,
        }


class ConversationSummary(BaseModel):
    participants: list[str] = Field(default_factory=list)
    active_topics: list[str] = Field(default_factory=list)
    open_loops: list[dict[str, Any]] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    memory_updates: list[MemoryFact] = Field(default_factory=list)

    @field_validator("open_loops", mode="before")
    @classmethod
    def normalize_string_open_loops(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        return [
            {"description": item} if isinstance(item, str) else item
            for item in value
        ]

    @field_validator("memory_updates", mode="before")
    @classmethod
    def discard_transient_memory_updates(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        transient_terms = (
            "不支持",
            "无法",
            "失败",
            "错误",
            "工具状态",
            "API 返回",
            "PendingAction",
        )

        def content(item: Any) -> str:
            if isinstance(item, str):
                return item
            if isinstance(item, dict):
                return str(item.get("memory_value") or "")
            return ""

        return [
            item
            for item in value
            if not any(term in content(item) for term in transient_terms)
        ]


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
