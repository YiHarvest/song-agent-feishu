"""日历 API。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import Field

from ..domain.results import ApplicationResult
from ..models import FeishuIdentity
from .dependencies import ApiRequestMeta, current_identity, user_request

router = APIRouter(prefix="/api/calendar/events", tags=["calendar"])


class CalendarPrepareRequest(ApiRequestMeta):
    summary: str = Field(min_length=1)
    start_time: str
    end_time: str | None = None
    timezone: str = "Asia/Shanghai"
    description: str | None = None
    location: str | None = None
    reminder_minutes: list[int] = Field(default_factory=lambda: [10])
    attendee_open_ids: list[str] = Field(default_factory=list)
    is_all_day: bool = False
    recurrence: str | None = None


class CalendarQueryRequest(ApiRequestMeta):
    query: str = ""
    start_time: datetime | None = None
    end_time: datetime | None = None
    event_id: str = ""
    page_size: int = Field(default=20, ge=1, le=30)


class CalendarUpdateRequest(ApiRequestMeta):
    summary: str | None = None
    description: str | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    timezone: str = "Asia/Shanghai"
    location: str | None = None
    reminder_minutes: list[int] | None = None
    recurrence: str | None = None
    recurrence_scope: str = "single"


class CalendarDeleteRequest(ApiRequestMeta):
    calendar_id: str = ""
    recurrence_scope: str = "single"


@router.get("")
async def list_events(
    request: Request,
    identity: Annotated[FeishuIdentity, Depends(current_identity)],
    query: str = "",
    event_id: str = "",
    page_size: int = 20,
) -> ApplicationResult:
    body = CalendarQueryRequest(
        query=query,
        event_id=event_id,
        page_size=page_size,
    )
    return await request.app.state.calendar_service.query(
        user_request(identity, body, text=query or "查询日程"),
        body.model_dump(exclude={"request_id", "chat_id", "thread_id"}),
    )


@router.post("/prepare")
async def prepare_create(
    body: CalendarPrepareRequest,
    request: Request,
    identity: Annotated[FeishuIdentity, Depends(current_identity)],
) -> ApplicationResult:
    payload = body.model_dump(exclude={"request_id", "chat_id", "thread_id"})
    return await request.app.state.calendar_service.prepare_create(
        user_request(identity, body, text=body.summary),
        payload,
    )


@router.patch("/{event_id}/prepare")
async def prepare_update(
    event_id: str,
    body: CalendarUpdateRequest,
    request: Request,
    identity: Annotated[FeishuIdentity, Depends(current_identity)],
) -> ApplicationResult:
    payload = body.model_dump(exclude={"request_id", "chat_id", "thread_id"})
    payload["event_id"] = event_id
    return await request.app.state.calendar_service.prepare_update(
        user_request(identity, body, text=f"修改日程 {event_id}"),
        payload,
    )


@router.delete("/{event_id}/prepare")
async def prepare_delete(
    event_id: str,
    body: CalendarDeleteRequest,
    request: Request,
    identity: Annotated[FeishuIdentity, Depends(current_identity)],
) -> ApplicationResult:
    payload = body.model_dump(exclude={"request_id", "chat_id", "thread_id"})
    payload["event_id"] = event_id
    return await request.app.state.calendar_service.prepare_delete(
        user_request(identity, body, text=f"删除日程 {event_id}"),
        payload,
    )
