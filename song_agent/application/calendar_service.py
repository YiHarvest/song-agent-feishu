"""日历应用服务。"""

from __future__ import annotations

import hashlib
import json
import logging
import time

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
from ..observability.context import current_trace_id
from ..services.audit import AuditService
from ..services.pending_actions import PendingActionService


class CalendarApplicationService:
    def __init__(
        self,
        oauth: FeishuOAuth,
        pending_actions: PendingActionService,
        openapi: FeishuOpenApi,
        *,
        default_timezone: str,
        audit: AuditService,
    ) -> None:
        self.oauth = oauth
        self.pending_actions = pending_actions
        self.openapi = openapi
        self.default_timezone = default_timezone
        self.audit = audit
        self.logger = logging.getLogger(__name__)

    async def create(
        self,
        request: UserRequest,
        arguments: dict,
        *,
        action_type: str = "calendar.create",
    ) -> ApplicationResult:
        """直连创建：校验通过且不影响他人时直接调用飞书 Calendar API。

        只有带参与人（attendee_open_ids）的创建仍走确认卡片；
        普通个人日程 / 循环日程 / 提醒一律直接执行。
        """
        started_at = time.monotonic()
        try:
            command = _parse_create_command(
                arguments,
                default_timezone=self.default_timezone,
                action_type=action_type,
            )
        except (ValidationError, ValueError) as error:
            return ApplicationResult(
                status="clarification_required",
                intent=action_type,
                message=str(error),
            )
        if command.attendee_open_ids:
            # 影响他人的创建必须经过确认
            return await self.prepare_create_confirmation(
                request,
                arguments,
                action_type=action_type,
            )
        required_scopes = ("calendar:calendar.event:create",)
        token = await self.oauth.get_valid_token_context(
            request.identity,
            required_scopes,
        )
        if not token:
            url = await self.oauth.create_authorization_url(
                request.identity,
                request.chat_id,
                required_scopes,
                original_request=resume_payload_json(request),
            )
            return ApplicationResult(
                status="authorization_required",
                intent=action_type,
                message="创建日程需要你本人授权。",
                authorization_url=url,
            )
        oauth_ms = int((time.monotonic() - started_at) * 1000)
        payload = command.model_dump(mode="json")
        idempotency_key = _build_create_idempotency_key(request, payload)
        try:
            result = await self.openapi.create_calendar_command(
                command,
                token,
                idempotency_key=idempotency_key,
            )
        except FeishuApiError as error:
            await self.audit.record(
                action_type,
                "failed_retryable" if error.retryable else "failed_final",
                tenant_key=request.identity.tenant_key,
                app_id=request.identity.app_id,
                principal_id=request.identity.subject_id,
                chat_id=request.chat_id,
                thread_id=request.thread_id,
                message_id=request.message_id,
                risk_level="high",
                payload_hash=idempotency_key,
                metadata={"error_code": str(error.code or "feishu_calendar_error")},
            )
            self.logger.error(
                "直连创建日程失败 intent=%s subject=%s trace_id=%s "
                "code=%s request_id=%s error=%s",
                action_type,
                request.identity.subject_id,
                current_trace_id(),
                error.code,
                error.request_id,
                error,
            )
            message = f"创建失败：{error}"
            if error.retryable:
                message += " 请稍后重试。"
            return ApplicationResult(
                status="error",
                intent=action_type,
                message=message,
            )
        total_ms = int((time.monotonic() - started_at) * 1000)
        self.logger.info(
            "直连创建日程 perf intent=%s subject=%s trace_id=%s "
            "oauth_ms=%d openapi_ms=%d total_ms=%d event_id=%s",
            action_type,
            request.identity.subject_id,
            current_trace_id(),
            oauth_ms,
            total_ms - oauth_ms,
            total_ms,
            result.get("event_id", ""),
        )
        await self.audit.record(
            action_type,
            "success",
            tenant_key=request.identity.tenant_key,
            app_id=request.identity.app_id,
            principal_id=request.identity.subject_id,
            chat_id=request.chat_id,
            thread_id=request.thread_id,
            message_id=request.message_id,
            risk_level="high",
            payload_hash=idempotency_key,
            metadata={
                "resource_type": "calendar.event",
                "resource_id": result.get("event_id", ""),
                "calendar_id": result.get("calendar_id", ""),
            },
        )
        resource_name = "提醒" if action_type == "reminder.create" else "日程"
        title = command.summary.replace("[", "").replace("]", "")
        url = result.get("url", "")
        resource = f"[{title}]({url})" if url else title
        time_text = ""
        if command.start_time is not None and command.end_time is not None:
            time_text = (
                f"\n时间：{command.start_time:%Y-%m-%d %H:%M}"
                f"–{command.end_time:%H:%M}"
            )
        return ApplicationResult(
            status="ok",
            intent=action_type,
            message=f"✅ 已创建{resource_name}：{resource}{time_text}",
            data=result,
        )

    async def prepare_create_confirmation(
        self,
        request: UserRequest,
        arguments: dict,
        *,
        action_type: str = "calendar.create",
    ) -> ApplicationResult:
        try:
            command = _parse_create_command(
                arguments,
                default_timezone=self.default_timezone,
                action_type=action_type,
            )
        except (ValidationError, ValueError) as error:
            return ApplicationResult(
                status="clarification_required",
                intent=action_type,
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
                intent=action_type,
                message="创建日程需要你本人授权。",
                authorization_url=url,
            )
        payload = command.model_dump(mode="json")
        idempotency_key = _build_create_idempotency_key(request, payload)
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


def _parse_create_command(
    arguments: dict,
    *,
    default_timezone: str,
    action_type: str,
) -> CalendarCreateCommand:
    """创建类操作的公共校验与标准化（直连与确认路径共用）。"""
    return normalize_calendar_create(
        CalendarCreateCommand.model_validate(arguments),
        default_timezone=default_timezone,
        default_duration_minutes=(
            1 if action_type == "reminder.create" else 60
        ),
        repair_nonpositive_duration=action_type == "reminder.create",
    )


def _build_create_idempotency_key(request: UserRequest, payload: dict) -> str:
    """创建类操作的幂等键：message_id + subject + 标准化 payload → SHA-256。

    直连与确认路径共用，保证同一请求无论走哪条路径都产生相同键。
    """
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    source_key = request.message_id or canonical
    return hashlib.sha256(
        (
            f"{request.identity.tenant_key}:{request.identity.app_id}:"
            f"{request.identity.subject_id}:{source_key}:{canonical}"
        ).encode()
    ).hexdigest()


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
