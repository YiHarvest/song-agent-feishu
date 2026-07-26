"""飞书任务 CRUD API。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import Field

from ..domain.results import ApplicationResult
from ..models import FeishuIdentity
from .dependencies import ApiRequestMeta, current_identity, user_request

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


class TaskCreateRequest(ApiRequestMeta):
    summary: str = Field(min_length=1, max_length=3000)
    description: str = ""
    start_time: datetime | None = None
    due_time: datetime | None = None
    is_all_day: bool = False
    assignee_open_ids: list[str] = Field(default_factory=list)
    follower_open_ids: list[str] = Field(default_factory=list)
    reminder_minutes: list[int] = Field(default_factory=list)
    repeat_rule: str = ""
    tasklist_guid: str = ""


class TaskUpdateRequest(ApiRequestMeta):
    fields: dict[str, Any] = Field(min_length=1)


class TaskActionRequest(ApiRequestMeta):
    pass


@router.get("")
async def list_tasks(
    request: Request,
    identity: Annotated[FeishuIdentity, Depends(current_identity)],
    query: str = "",
    task_guid: str = "",
    completed: bool | None = None,
    page_size: int = 50,
) -> ApplicationResult:
    meta = ApiRequestMeta()
    return await request.app.state.task_service.query(
        user_request(identity, meta, text=query or "查询任务"),
        {
            "query": query,
            "task_guid": task_guid,
            "completed": completed,
            "page_size": page_size,
        },
    )


@router.post("/prepare")
async def prepare_create(
    body: TaskCreateRequest,
    request: Request,
    identity: Annotated[FeishuIdentity, Depends(current_identity)],
) -> ApplicationResult:
    return await request.app.state.task_service.prepare_create(
        user_request(identity, body, text=body.summary),
        body.model_dump(exclude={"request_id", "chat_id", "thread_id"}),
    )


@router.patch("/{task_guid}/prepare")
async def prepare_update(
    task_guid: str,
    body: TaskUpdateRequest,
    request: Request,
    identity: Annotated[FeishuIdentity, Depends(current_identity)],
) -> ApplicationResult:
    return await request.app.state.task_service.prepare_update(
        user_request(identity, body, text=f"修改任务 {task_guid}"),
        {"task_guid": task_guid, "fields": body.fields},
    )


@router.post("/{task_guid}/complete/prepare")
async def prepare_complete(
    task_guid: str,
    body: TaskActionRequest,
    request: Request,
    identity: Annotated[FeishuIdentity, Depends(current_identity)],
) -> ApplicationResult:
    return await request.app.state.task_service.prepare_complete(
        user_request(identity, body, text=f"完成任务 {task_guid}"),
        {"task_guid": task_guid},
    )


@router.delete("/{task_guid}/prepare")
async def prepare_delete(
    task_guid: str,
    body: TaskActionRequest,
    request: Request,
    identity: Annotated[FeishuIdentity, Depends(current_identity)],
) -> ApplicationResult:
    return await request.app.state.task_service.prepare_delete(
        user_request(identity, body, text=f"删除任务 {task_guid}"),
        {"task_guid": task_guid},
    )
