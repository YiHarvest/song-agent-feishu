"""飞书日历提醒 CRUD API。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import Field

from ..domain.results import ApplicationResult
from ..models import FeishuIdentity
from .dependencies import ApiRequestMeta, current_identity, user_request

router = APIRouter(prefix="/api/reminders", tags=["reminders"])


class ReminderCreateRequest(ApiRequestMeta):
    summary: str = Field(min_length=1, max_length=255)
    start_time: datetime
    end_time: datetime | None = None
    timezone: str = "Asia/Shanghai"
    description: str = ""
    recurrence: str | None = None


class ReminderCancelRequest(ApiRequestMeta):
    calendar_id: str = ""
    recurrence_scope: str = "single"


@router.get("")
async def list_reminders(
    request: Request,
    identity: Annotated[FeishuIdentity, Depends(current_identity)],
    query: str = "",
    event_id: str = "",
    page_size: int = 20,
) -> ApplicationResult:
    meta = ApiRequestMeta()
    return await request.app.state.reminder_service.query(
        user_request(identity, meta, text=query or "查询提醒"),
        {"query": query, "event_id": event_id, "page_size": page_size},
    )


@router.post("/prepare")
async def prepare_create(
    body: ReminderCreateRequest,
    request: Request,
    identity: Annotated[FeishuIdentity, Depends(current_identity)],
) -> ApplicationResult:
    return await request.app.state.reminder_service.prepare_create(
        user_request(identity, body, text=body.summary),
        body.model_dump(exclude={"request_id", "chat_id", "thread_id"}),
    )


@router.delete("/{event_id}/prepare")
async def prepare_cancel(
    event_id: str,
    body: ReminderCancelRequest,
    request: Request,
    identity: Annotated[FeishuIdentity, Depends(current_identity)],
) -> ApplicationResult:
    payload = body.model_dump(exclude={"request_id", "chat_id", "thread_id"})
    payload["event_id"] = event_id
    return await request.app.state.reminder_service.prepare_cancel(
        user_request(identity, body, text=f"取消提醒 {event_id}"),
        payload,
    )
