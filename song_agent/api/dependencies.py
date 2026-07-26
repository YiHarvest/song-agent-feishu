"""React/API 身份依赖。"""

import uuid
from typing import Annotated

from fastapi import Header, HTTPException, Request
from pydantic import BaseModel

from ..domain.intents import UserRequest
from ..models import FeishuIdentity


class ApiRequestMeta(BaseModel):
    request_id: str = ""
    chat_id: str = ""
    thread_id: str = ""


def user_request(
    identity: FeishuIdentity,
    meta: ApiRequestMeta,
    *,
    text: str,
) -> UserRequest:
    return UserRequest(
        identity=identity,
        text=text,
        source="api",
        chat_id=meta.chat_id,
        thread_id=meta.thread_id,
        message_id=meta.request_id or str(uuid.uuid4()),
    )


async def current_identity(
    request: Request,
    x_principal_id: Annotated[str, Header()] = "",
    x_open_id: Annotated[str, Header()] = "",
    x_tenant_key: Annotated[str, Header()] = "",
    x_user_id: Annotated[str, Header()] = "",
    x_union_id: Annotated[str, Header()] = "",
) -> FeishuIdentity:
    if not x_principal_id or not x_open_id:
        raise HTTPException(401, "缺少 X-Principal-Id 或 X-Open-Id")
    settings = request.app.state.settings
    return FeishuIdentity(
        tenant_key=x_tenant_key,
        app_id=settings.feishu_app_id,
        open_id=x_open_id,
        user_id=x_user_id,
        union_id=x_union_id or (x_principal_id if x_principal_id.startswith("on_") else ""),
    )
