"""活动与集成状态 API。"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from ..models import FeishuIdentity
from .dependencies import current_identity

router = APIRouter(prefix="/api", tags=["activity"])


@router.get("/activity")
async def activity(
    request: Request,
    identity: Annotated[FeishuIdentity, Depends(current_identity)],
) -> list[dict[str, Any]]:
    actions = await request.app.state.pending_action_service.list(identity)
    return [
        {
            "action_id": action.action_id,
            "action_type": action.action_type,
            "status": action.status.upper(),
            "created_at": action.created_at,
            "error_code": action.error_code,
            "error_message": action.error_message,
        }
        for action in actions
    ]


@router.get("/integrations/feishu/status")
async def feishu_status(
    request: Request,
    identity: Annotated[FeishuIdentity, Depends(current_identity)],
) -> dict[str, Any]:
    token = await request.app.state.oauth.get_valid_token_context(identity, ())
    return {
        "authorized": token is not None,
        "subject_id": identity.subject_id,
        "open_id": identity.open_id,
        "scopes": sorted(token.scopes) if token else [],
    }
