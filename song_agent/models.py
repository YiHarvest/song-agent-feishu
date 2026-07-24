"""
数据模型定义。

定义消息、任务、记录、令牌等 Pydantic 模型。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Priority = Literal["A", "B", "C"]
TaskStatus = Literal["pending", "completed", "partial", "not_done", "unconfirmed"]
RepeatType = Literal["none", "daily", "weekdays", "weekly"]


class FeishuIdentity(BaseModel):
    """标准化飞书身份。`subject_id` 在我们的数据库中是稳定的。"""

    model_config = {"frozen": True}

    tenant_key: str = ""
    app_id: str
    open_id: str
    user_id: str = ""
    union_id: str = ""

    @property
    def subject_id(self) -> str:
        return self.union_id or self.user_id or self.open_id


class ConversationKey(BaseModel):
    """飞书会话中单个用户对话的隔离键。"""

    model_config = {"frozen": True}

    tenant_key: str = ""
    app_id: str
    chat_id: str
    thread_id: str = ""
    subject_id: str

    def serialize(self) -> str:
        return ":".join(
            (
                self.tenant_key or "_",
                self.app_id,
                self.chat_id,
                self.thread_id or "_",
                self.subject_id,
            )
        )


class IncomingMessage(BaseModel):
    message_id: str
    event_id: str = ""
    tenant_key: str = ""
    app_id: str = ""
    user_id: str
    open_id: str = ""
    tenant_user_id: str = ""
    union_id: str = ""
    chat_id: str
    thread_id: str = ""
    root_id: str = ""
    chat_type: Literal["p2p", "group"]
    message_type: str
    text: str

    def identity(self, default_app_id: str = "") -> FeishuIdentity:
        return FeishuIdentity(
            tenant_key=self.tenant_key,
            app_id=self.app_id or default_app_id,
            open_id=self.open_id or self.user_id,
            user_id=self.tenant_user_id,
            union_id=self.union_id,
        )

    def conversation_key(self, default_app_id: str = "") -> ConversationKey:
        identity = self.identity(default_app_id)
        return ConversationKey(
            tenant_key=identity.tenant_key,
            app_id=identity.app_id,
            chat_id=self.chat_id,
            thread_id=self.thread_id or self.root_id,
            subject_id=identity.subject_id,
        )

    def chat_queue_key(self, default_app_id: str = "") -> str:
        """序列化同一聊天/会话中的所有消息，不区分发送者。"""
        return ":".join(
            (
                self.tenant_key or "_",
                self.app_id or default_app_id,
                self.chat_id,
                self.thread_id or self.root_id or "_",
            )
        )


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
    tenant_key: str = ""
    app_id: str = ""
    chat_id: str
    thread_id: str = ""
    user_id: str
    plan_status: Literal["draft", "confirmed"]
    tasks: list[PlanTask]
    created_at: str
    updated_at: str
    review: DailyReview | None = None


class OAuthToken(BaseModel):
    user_id: str
    tenant_key: str = ""
    app_id: str = ""
    open_id: str = ""
    tenant_user_id: str = ""
    union_id: str = ""
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


@dataclass(frozen=True, slots=True)
class UserTokenContext:
    """不可变授权上下文，显式传递给每个用户级 API 调用。"""

    tenant_key: str
    app_id: str
    subject_id: str
    open_id: str
    access_token: str
    expires_at: datetime
    scopes: frozenset[str]


PendingActionStatus = Literal[
    "awaiting_confirmation",
    "confirmed",
    "executing",
    "succeeded",
    "failed_retryable",
    "failed_final",
    "unknown_remote_state",
    "cancelled",
    "expired",
]


class PendingAction(BaseModel):
    action_id: str
    tenant_key: str = ""
    app_id: str
    chat_id: str
    thread_id: str = ""
    creator_subject_id: str
    creator_open_id: str
    action_type: str
    payload: dict[str, Any]
    payload_hash: str
    source_message_id: str
    status: PendingActionStatus = "awaiting_confirmation"
    expires_at: int
    created_at: int
    consumed_at: int | None = None
    attempt_count: int = 0
    remote_resource_id: str = ""
    remote_request_id: str = ""


class ScheduledJob(BaseModel):
    job_id: str
    tenant_key: str = ""
    app_id: str
    principal_id: str
    job_type: str
    payload: dict[str, Any]
    timezone: str
    cron_expression: str = ""
    run_at: int
    status: str
    attempts: int = 0
    fencing_token: int | None = None


IntentName = Literal[
    "plan.create",
    "calendar.create",
    "review.create",
    "document.create",
    "document.append",
    "chat.reply",
    "unknown",
]


class ActionIntentOutput(BaseModel):
    """严格的 LLM 路由结果。仅描述请求，不执行。"""

    intent: IntentName
    confidence: float = Field(ge=0, le=1)
    requires_confirmation: bool
    parameters: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
