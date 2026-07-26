"""待确认动作 API。"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request

from ..domain.results import ApplicationResult
from ..models import FeishuIdentity, PendingAction
from .dependencies import current_identity

router = APIRouter(prefix="/api/pending-actions", tags=["pending-actions"])


def serialize(action: PendingAction) -> dict[str, Any]:
    data = action.model_dump(mode="json")
    data["status"] = action.status.upper()
    return data


@router.get("")
async def list_actions(
    request: Request,
    identity: Annotated[FeishuIdentity, Depends(current_identity)],
) -> list[dict[str, Any]]:
    return [
        serialize(action)
        for action in await request.app.state.pending_action_service.list(identity)
    ]


@router.get("/{action_id}")
async def get_action(
    action_id: str,
    request: Request,
    identity: Annotated[FeishuIdentity, Depends(current_identity)],
) -> dict[str, Any]:
    action = await request.app.state.pending_action_service.get(identity, action_id)
    if not action:
        raise HTTPException(404, "待确认操作不存在")
    return serialize(action)


@router.post("/{action_id}/confirm")
async def confirm_action(
    action_id: str,
    request: Request,
    identity: Annotated[FeishuIdentity, Depends(current_identity)],
) -> ApplicationResult:
    return await request.app.state.pending_action_service.confirm(identity, action_id)


@router.post("/{action_id}/cancel")
async def cancel_action(
    action_id: str,
    request: Request,
    identity: Annotated[FeishuIdentity, Depends(current_identity)],
) -> ApplicationResult:
    return await request.app.state.pending_action_service.cancel(identity, action_id)


@router.post("/{action_id}/retry")
async def retry_action(
    action_id: str,
    request: Request,
    identity: Annotated[FeishuIdentity, Depends(current_identity)],
) -> ApplicationResult:
    return await request.app.state.pending_action_service.retry(identity, action_id)
