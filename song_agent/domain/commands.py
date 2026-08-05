"""严格业务命令模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class CalendarCreateCommand(BaseModel):
    summary: str = Field(min_length=1, max_length=255)
    start_time: datetime | None = None
    end_time: datetime | None = None
    timezone: str = "Asia/Shanghai"
    description: str | None = Field(default=None, max_length=4096)
    location: str | None = Field(default=None, max_length=512)
    reminder_minutes: list[int] = Field(default_factory=lambda: [10])
    attendee_open_ids: list[str] = Field(default_factory=list)
    is_all_day: bool = False
    recurrence: str | None = None

    @field_validator("summary")
    @classmethod
    def strip_summary(cls, value: str) -> str:
        return value.strip()

    @field_validator("reminder_minutes", mode="before")
    @classmethod
    def normalize_single_reminder(cls, value: object) -> object:
        if isinstance(value, int) and not isinstance(value, bool):
            return [value]
        return value

    @field_validator("reminder_minutes")
    @classmethod
    def validate_reminders(cls, value: list[int]) -> list[int]:
        if any(minutes < 0 or minutes > 20160 for minutes in value):
            raise ValueError("提醒时间必须在 0 到 20160 分钟之间")
        return list(dict.fromkeys(value))



class CalendarQueryCommand(BaseModel):
    query: str = Field(default="", max_length=255)
    start_time: datetime | None = None
    end_time: datetime | None = None
    event_id: str = ""
    page_size: int = Field(default=20, ge=1, le=30)


class CalendarUpdateCommand(BaseModel):
    event_id: str = Field(min_length=1)
    calendar_id: str = ""
    summary: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None, max_length=4096)
    start_time: datetime | None = None
    end_time: datetime | None = None
    timezone: str = "Asia/Shanghai"
    location: str | None = Field(default=None, max_length=512)
    reminder_minutes: list[int] | None = None
    recurrence: str | None = None
    recurrence_scope: Literal["single", "all", "future"] = "single"


class CalendarDeleteCommand(BaseModel):
    event_id: str = Field(min_length=1)
    calendar_id: str = ""
    recurrence_scope: Literal["single", "all", "future"] = "single"


class TaskCreateCommand(BaseModel):
    summary: str = Field(min_length=1, max_length=3000)
    description: str = Field(default="", max_length=3000)
    start_time: datetime | None = None
    due_time: datetime | None = None
    is_all_day: bool = False
    assignee_open_ids: list[str] = Field(default_factory=list)
    follower_open_ids: list[str] = Field(default_factory=list)
    reminder_minutes: list[int] = Field(default_factory=list)
    repeat_rule: str = ""
    tasklist_guid: str = ""


class TaskQueryCommand(BaseModel):
    query: str = Field(default="", max_length=3000)
    task_guid: str = ""
    completed: bool | None = None
    page_size: int = Field(default=50, ge=1, le=100)


class TaskTimePatch(BaseModel):
    timestamp: str
    is_all_day: bool = False


class TaskUpdateFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str | None = Field(default=None, max_length=3000)
    description: str | None = Field(default=None, max_length=3000)
    start: TaskTimePatch | None = None
    due: TaskTimePatch | None = None
    repeat_rule: str | None = None
    completed_at: str | None = None


class TaskUpdateCommand(BaseModel):
    task_guid: str = Field(min_length=1)
    fields: TaskUpdateFields

    @model_validator(mode="after")
    def require_update_field(self) -> TaskUpdateCommand:
        if not self.fields.model_fields_set:
            raise ValueError("任务更新至少需要一个字段")
        return self


class TaskTargetCommand(BaseModel):
    task_guid: str = Field(min_length=1)


class ReminderQueryCommand(CalendarQueryCommand):
    pass


class ReminderCancelCommand(CalendarDeleteCommand):
    pass
