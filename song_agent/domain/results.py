"""应用层和执行器结果。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ApplicationResult(BaseModel):
    status: Literal[
        "ok",
        "clarification_required",
        "authorization_required",
        "awaiting_confirmation",
        "unsupported",
        "error",
    ]
    message: str
    intent: str = ""
    action_id: str = ""
    authorization_url: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class ExecutionResult(BaseModel):
    status: Literal["succeeded", "failed_retryable", "failed_final"]
    result: dict[str, Any] = Field(default_factory=dict)
    error_code: str = ""
    error_message: str = ""
    remote_request_id: str = ""


class ExecutionContext(BaseModel):
    worker_id: str
