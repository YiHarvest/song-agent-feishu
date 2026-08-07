"""ReAct 决策和工具观察的结构化模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class AgentDecision(BaseModel):
    type: Literal["final_answer", "ask_user", "tool_call"]
    content: str = Field(default="", max_length=12000)
    tool_name: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)
    decision_summary: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def validate_shape(self) -> AgentDecision:
        if self.type == "tool_call" and not self.tool_name:
            raise ValueError("tool_call requires tool_name")
        if self.type != "tool_call" and not self.content:
            raise ValueError(f"{self.type} requires content")
        return self


class ToolResult(BaseModel):
    status: Literal["ok", "denied", "error"]
    summary: str = Field(max_length=2000)
    terminal: bool = False
    response: str = Field(default="", max_length=12000)
    # prepare 类工具创建的待确认 Action ID；terminal 时由 Runtime 透传给 AgentResult
    pending_action_ids: tuple[str, ...] = ()


class AgentResult(BaseModel):
    status: Literal["completed", "awaiting_user", "failed"]
    response: str = Field(max_length=12000)
    step_count: int
    tool_call_count: int
    error_code: str = ""
    # 由 terminal ToolResult 透传的待确认 Action ID，供适配层转 awaiting_confirmation
    pending_action_ids: tuple[str, ...] = ()
