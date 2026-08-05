"""统一聊天 API。"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from ..domain.intents import UserRequest
from ..domain.results import ApplicationResult
from ..models import FeishuIdentity
from .dependencies import current_identity

router = APIRouter(prefix="/api/chat", tags=["chat"])


class ChatRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)
    request_id: str = ""
    chat_id: str = ""
    thread_id: str = ""


@router.post("")
async def chat(
    body: ChatRequest,
    request: Request,
    identity: Annotated[FeishuIdentity, Depends(current_identity)],
) -> ApplicationResult:
    return await request.app.state.dispatcher.dispatch(
        UserRequest(
            identity=identity,
            text=body.text,
            source="react",
            chat_id=body.chat_id,
            thread_id=body.thread_id,
            message_id=body.request_id or str(uuid.uuid4()),
        )
    )
