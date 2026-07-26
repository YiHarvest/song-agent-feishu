"""飞书任务确定性应用服务。"""

from __future__ import annotations

import hashlib
import json

from pydantic import ValidationError

from ..domain.commands import (
    TaskCreateCommand,
    TaskQueryCommand,
    TaskTargetCommand,
    TaskUpdateCommand,
)
from ..domain.intents import UserRequest
from ..domain.results import ApplicationResult
from ..feishu.oauth import FeishuOAuth
from ..feishu.openapi import FeishuApiError, FeishuOpenApi
from ..services.pending_actions import PendingActionService
from .calendar_service import _incoming_message


class TaskApplicationService:
    def __init__(
        self,
        oauth: FeishuOAuth,
        pending_actions: PendingActionService,
        openapi: FeishuOpenApi,
    ) -> None:
        self.oauth = oauth
        self.pending_actions = pending_actions
        self.openapi = openapi

    async def prepare_create(self, request: UserRequest, arguments: dict) -> ApplicationResult:
        try:
            command = TaskCreateCommand.model_validate(arguments)
            if command.start_time and command.due_time:
                if command.start_time > command.due_time:
                    raise ValueError("任务开始时间不能晚于截止时间")
            if (command.repeat_rule or command.reminder_minutes) and not command.due_time:
                raise ValueError("设置重复或提醒时必须提供截止时间")
            if not command.assignee_open_ids:
                command.assignee_open_ids = [request.identity.open_id]
        except (ValidationError, ValueError) as error:
            return _clarification("task.create", error)
        return await self._prepare(
            request,
            "task.create",
            command.model_dump(mode="json"),
            "任务已准备，等待确认。",
        )

    async def query(self, request: UserRequest, arguments: dict) -> ApplicationResult:
        try:
            command = TaskQueryCommand.model_validate(arguments)
        except ValidationError as error:
            return _clarification("task.query", error)
        token = await self._token(request, write=False)
        if isinstance(token, ApplicationResult):
            return token
        try:
            data = await self.openapi.query_tasks(command, token)
        except FeishuApiError as error:
            return ApplicationResult(
                status="error",
                intent="task.query",
                message=f"查询任务失败：{error}",
            )
        return ApplicationResult(
            status="ok",
            intent="task.query",
            message="任务查询完成。",
            data=data,
        )

    async def prepare_update(self, request: UserRequest, arguments: dict) -> ApplicationResult:
        try:
            command = TaskUpdateCommand.model_validate(arguments)
        except ValidationError as error:
            return _clarification("task.update", error)
        return await self._prepare(
            request,
            "task.update",
            command.model_dump(mode="json"),
            "任务修改已准备，等待确认。",
        )

    async def prepare_complete(self, request: UserRequest, arguments: dict) -> ApplicationResult:
        return await self._prepare_target(request, arguments, "task.complete", "任务完成操作")

    async def prepare_delete(self, request: UserRequest, arguments: dict) -> ApplicationResult:
        return await self._prepare_target(request, arguments, "task.delete", "任务删除操作")

    async def _prepare_target(
        self,
        request: UserRequest,
        arguments: dict,
        action_type: str,
        label: str,
    ) -> ApplicationResult:
        try:
            command = TaskTargetCommand.model_validate(arguments)
        except ValidationError as error:
            return _clarification(action_type, error)
        return await self._prepare(
            request,
            action_type,
            command.model_dump(mode="json"),
            f"{label}已准备，等待确认。",
        )

    async def _prepare(
        self,
        request: UserRequest,
        action_type: str,
        payload: dict,
        message: str,
    ) -> ApplicationResult:
        token = await self._token(request, write=True)
        if isinstance(token, ApplicationResult):
            return token
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        key = hashlib.sha256(
            (
                f"{request.identity.tenant_key}:{request.identity.app_id}:"
                f"{request.identity.subject_id}:{request.message_id}:{action_type}:{canonical}"
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

    async def _token(
        self,
        request: UserRequest,
        *,
        write: bool,
    ):
        scope = "task:task:write" if write else "task:task:read"
        token = await self.oauth.get_valid_token_context(request.identity, (scope,))
        if token:
            return token
        url = await self.oauth.create_authorization_url(
            request.identity,
            request.chat_id,
            (scope,),
            original_request=request.text,
        )
        return ApplicationResult(
            status="authorization_required",
            intent="task",
            message="飞书任务操作需要你本人授权。",
            authorization_url=url,
        )


def _clarification(intent: str, error: Exception) -> ApplicationResult:
    return ApplicationResult(
        status="clarification_required",
        intent=intent,
        message=str(error),
    )
