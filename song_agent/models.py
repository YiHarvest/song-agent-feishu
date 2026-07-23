"""
数据模型定义。

定义消息、任务、记录、令牌等 Pydantic 模型。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Priority = Literal["A", "B", "C"]
TaskStatus = Literal["pending", "completed", "partial", "not_done", "unconfirmed"]
RepeatType = Literal["none", "daily", "weekdays", "weekly"]


class IncomingMessage(BaseModel):
    message_id: str
    user_id: str
    chat_id: str
    chat_type: Literal["p2p", "group"]
    message_type: str
    text: str


class PlanTask(BaseModel):
    id: str
    priority: Priority
    title: str
    start_time: str | None = None
    end_time: str | None = None
    repeat: RepeatType = "none"
    status: TaskStatus = "pending"
    completion_ratio: float | None = None
    calendar_event_id: str | None = None


class DailyReview(BaseModel):
    created_at: str
    completion_rate: int
    summary: str
    insights: list[str] = Field(default_factory=list)


class DailyRecord(BaseModel):
    key: str
    date: str
    chat_id: str
    user_id: str
    plan_status: Literal["draft", "confirmed"]
    tasks: list[PlanTask]
    created_at: str
    updated_at: str
    review: DailyReview | None = None


class OAuthToken(BaseModel):
    user_id: str
    access_token: str
    refresh_token: str
    expires_at: int
    refresh_expires_at: int
    scope: str


class ParsedPlanTask(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    priority: Priority
    start_time: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    end_time: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    repeat: RepeatType = "none"


class PlanOutput(BaseModel):
    tasks: list[ParsedPlanTask] = Field(max_length=30)


class ReviewUpdate(BaseModel):
    task_id: str
    status: Literal["completed", "partial", "not_done", "unconfirmed"]
    completion_ratio: float | None = Field(default=None, ge=0, le=100)


class ReviewOutput(BaseModel):
    updates: list[ReviewUpdate]
    summary: str = Field(min_length=1, max_length=1000)
    insights: list[str] = Field(default_factory=list, max_length=5)


class DocumentOutput(BaseModel):
    action: Literal["create", "append"] = "create"
    target_title: str | None = Field(default=None, max_length=100)
    title: str | None = Field(default=None, min_length=1, max_length=100)
    markdown: str = Field(min_length=1, max_length=100_000)


class DocumentBinding(BaseModel):
    chat_id: str
    user_id: str
    title: str
    token: str
    url: str


class IntentOutput(BaseModel):
    intent: Literal["plan", "reminder", "review", "document", "chat", "unknown"]


class ChatOutput(BaseModel):
    reply: str = Field(min_length=1, max_length=2000)
