"""日历应用服务。"""

from __future__ import annotations

import hashlib
import json

from pydantic import ValidationError

from ..domain.commands import (
    CalendarCreateCommand,
    CalendarDeleteCommand,
    CalendarQueryCommand,
    CalendarUpdateCommand,
)
from ..domain.intents import UserRequest
from ..domain.policies import normalize_calendar_create
from ..domain.results import ApplicationResult
from ..feishu.oauth import FeishuOAuth
from ..feishu.openapi import FeishuApiError, FeishuOpenApi
from ..models import IncomingMessage
from ..modules.oauth.resume_models import resume_payload_json
from ..services.pending_actions import PendingActionService


class CalendarApplicationService:
    def __init__(
        self,
        oauth: FeishuOAuth,
        pending_actions: PendingActionService,
        openapi: FeishuOpenApi,
        *,
        default_timezone: str,
    ) -> None:
        self.oauth = oauth
        self.pending_actions = pending_actions
        self.openapi = openapi
        self.default_timezone = default_timezone

    async def prepare_create(
        self,
        request: UserRequest,
        arguments: dict,
        *,
        action_type: str = "calendar.create",
    ) -> ApplicationResult:
        try:
            command = normalize_calendar_create(
                CalendarCreateCommand.model_validate(arguments),
                default_timezone=self.default_timezone,
                default_duration_minutes=(
                    1 if action_type == "reminder.create" else 60
                ),
                repair_nonpositive_duration=action_type == "reminder.create",
            )
        except (ValidationError, ValueError) as error:
            return ApplicationResult(
                status="clarification_required",
                intent="calendar.create",
                message=str(error),
            )
        required_scopes = ("calendar:calendar.event:create",)
        token = await self.oauth.get_valid_token_context(request.identity, required_scopes)
        if not token:
            url = await self.oauth.create_authorization_url(
                request.identity,
                request.chat_id,
                required_scopes,
                original_request=resume_payload_json(request),
            )
            return ApplicationResult(
                status="authorization_required",
                intent="calendar.create",
                message="创建日程需要你本人授权。",
                authorization_url=url,
            )
        payload = command.model_dump(mode="json")
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        source_key = request.message_id or canonical
        idempotency_key = hashlib.sha256(
            (
                f"{request.identity.tenant_key}:{request.identity.app_id}:"
                f"{request.identity.subject_id}:{source_key}:{canonical}"
            ).encode()
        ).hexdigest()
        action = await self.pending_actions.create_action(
            _incoming_message(request),
            action_type=action_type,
            payload=payload,
            idempotency_key=idempotency_key,
            source=request.source,
        )
        return ApplicationResult(
            status="awaiting_confirmation",
            intent=action_type,
            action_id=action.action_id,
            message="日程草稿已准备，等待确认。",
            data=payload,
        )

    async def query(
        self,
        request: UserRequest,
        arguments: dict,
    ) -> ApplicationResult:
        try:
            command = CalendarQueryCommand.model_validate(arguments)
        except ValidationError as error:
            return ApplicationResult(
                status="clarification_required",
                intent="calendar.query",
                message=str(error),
            )
        token = await self.oauth.get_valid_token_context(
            request.identity,
            ("calendar:calendar.event:read",),
        )
        if not token:
            url = await self.oauth.create_authorization_url(
                request.identity,
                request.chat_id,
                ("calendar:calendar.event:read",),
                original_request=resume_payload_json(request),
            )
            return ApplicationResult(
                status="authorization_required",
                intent="calendar.query",
                message="查询日程需要你本人授权。",
                authorization_url=url,
            )
        try:
            data = await self.openapi.query_calendar(command, token)
        except FeishuApiError as error:
            return ApplicationResult(
                status="error",
                intent="calendar.query",
                message=f"查询日程失败：{error}",
            )
        return ApplicationResult(
            status="ok",
            intent="calendar.query",
            message="日程查询完成。",
            data=data,
        )

    async def prepare_update(
        self,
        request: UserRequest,
        arguments: dict,
    ) -> ApplicationResult:
        try:
            command = CalendarUpdateCommand.model_validate(arguments)
            if (command.start_time is None) != (command.end_time is None):
                raise ValueError("修改日程时间时必须同时提供开始和结束时间")
            if (
                command.start_time
                and command.end_time
                and command.end_time <= command.start_time
            ):
                raise ValueError("日程结束时间必须晚于开始时间")
        except (ValidationError, ValueError) as error:
            return ApplicationResult(
                status="clarification_required",
                intent="calendar.update",
                message=str(error),
            )
        return await self._prepare_write(
            request,
            action_type="calendar.update",
            payload=command.model_dump(mode="json"),
            message="日程修改已准备，等待确认。",
        )

    async def prepare_delete(
        self,
        request: UserRequest,
        arguments: dict,
        *,
        action_type: str = "calendar.delete",
    ) -> ApplicationResult:
        try:
            command = CalendarDeleteCommand.model_validate(arguments)
        except ValidationError as error:
            return ApplicationResult(
                status="clarification_required",
                intent=action_type,
                message=str(error),
            )
        return await self._prepare_write(
            request,
            action_type=action_type,
            payload=command.model_dump(mode="json"),
            message="删除操作已准备，等待确认。",
        )

    async def _prepare_write(
        self,
        request: UserRequest,
        *,
        action_type: str,
        payload: dict,
        message: str,
    ) -> ApplicationResult:
        scopes = ("calendar:calendar",)
        token = await self.oauth.get_valid_token_context(request.identity, scopes)
        if not token:
            url = await self.oauth.create_authorization_url(
                request.identity,
                request.chat_id,
                scopes,
                original_request=request.text,
            )
            return ApplicationResult(
                status="authorization_required",
                intent=action_type,
                message="修改日程需要你本人授权。",
                authorization_url=url,
            )
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        key = hashlib.sha256(
            (
                f"{request.identity.tenant_key}:{request.identity.app_id}:"
                f"{request.identity.subject_id}:{request.message_id}:{action_type}:"
                f"{canonical}"
            ).encode()
        ).hexdigest()
        action = await self.pending_actions.create_action(
            _incoming_message(request),
            action_type=action_type,
            payload=payload,
            idempotency_key=key,
            source=request.source,
        )
        return ApplicationResult(
            status="awaiting_confirmation",
            intent=action_type,
            action_id=action.action_id,
            message=message,
            data=payload,
        )


def _incoming_message(request: UserRequest) -> IncomingMessage:
    return IncomingMessage(
        message_id=request.message_id,
        event_id=request.event_id,
        tenant_key=request.identity.tenant_key,
        app_id=request.identity.app_id,
        user_id=request.identity.subject_id,
        open_id=request.identity.open_id,
        tenant_user_id=request.identity.user_id,
        union_id=request.identity.union_id,
        chat_id=request.chat_id,
        thread_id=request.thread_id,
        root_id="",
        chat_type="p2p",
        message_type="text",
        text=request.text,
    )
