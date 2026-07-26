"""calendar.create 确定性执行器。"""

from __future__ import annotations

import logging

from pydantic import ValidationError

from ..domain.commands import CalendarCreateCommand
from ..domain.results import ExecutionContext, ExecutionResult
from ..feishu.oauth import FeishuOAuth
from ..feishu.openapi import FeishuApiError, FeishuOpenApi
from ..feishu.transport import FeishuTransport
from ..models import FeishuIdentity, PendingAction
from ..services.audit import AuditService
from ..store import SqliteStore


class CalendarCreateExecutor:
    action_type = "calendar.create"

    def __init__(
        self,
        store: SqliteStore,
        oauth: FeishuOAuth,
        openapi: FeishuOpenApi,
        audit: AuditService,
        transport: FeishuTransport,
    ) -> None:
        self.store = store
        self.oauth = oauth
        self.openapi = openapi
        self.audit = audit
        self.transport = transport
        self.logger = logging.getLogger(__name__)

    async def execute(
        self,
        pending_action: PendingAction,
        context: ExecutionContext,
    ) -> ExecutionResult:
        claimed = await self.store.claim_action_execution(
            pending_action.action_id,
            worker_id=context.worker_id,
        )
        if not claimed:
            return ExecutionResult(
                status="failed_final",
                error_code="claim_failed",
                error_message="动作未处于可执行状态",
            )
        try:
            command = CalendarCreateCommand.model_validate(pending_action.payload)
        except ValidationError as error:
            return await self._finish_failure(
                pending_action,
                retryable=False,
                code="invalid_payload",
                message=str(error),
            )
        identity = FeishuIdentity(
            tenant_key=pending_action.tenant_key,
            app_id=pending_action.app_id,
            open_id=pending_action.creator_open_id,
            union_id=pending_action.creator_subject_id,
        )
        token = await self.oauth.get_valid_token_context(
            identity,
            ("calendar:calendar.event:create",),
        )
        if not token:
            return await self._finish_failure(
                pending_action,
                retryable=False,
                code="authorization_required",
                message="用户授权不可用或缺少 calendar:calendar.event:create",
            )
        try:
            result = await self.openapi.create_calendar_command(
                command,
                token,
                idempotency_key=pending_action.idempotency_key or pending_action.payload_hash,
            )
        except FeishuApiError as error:
            return await self._finish_failure(
                pending_action,
                retryable=error.retryable,
                code=str(error.code or "feishu_calendar_error"),
                message=str(error),
                remote_request_id=error.request_id,
            )
        await self.store.complete_pending_action(
            pending_action.action_id,
            status="succeeded",
            result=result,
            remote_resource_id=result["event_id"],
            remote_request_id=result.get("request_id", ""),
        )
        await self.audit.record(
            self.action_type,
            "success",
            tenant_key=pending_action.tenant_key,
            app_id=pending_action.app_id,
            principal_id=pending_action.creator_subject_id,
            chat_id=pending_action.chat_id,
            thread_id=pending_action.thread_id,
            action_id=pending_action.action_id,
            risk_level="high",
            payload_hash=pending_action.payload_hash,
            metadata={
                "resource_type": "calendar.event",
                "resource_id": result["event_id"],
                "calendar_id": result["calendar_id"],
            },
        )
        if pending_action.chat_id:
            resource_name = (
                "提醒" if pending_action.action_type == "reminder.create" else "日程"
            )
            title = command.summary.replace("[", "").replace("]", "")
            url = result.get("url", "")
            resource = f"[{title}]({url})" if url else title
            await self.transport.send_markdown(
                pending_action.chat_id,
                f"✅ 已创建{resource_name}：{resource}",
            )
        return ExecutionResult(status="succeeded", result=result)

    async def _finish_failure(
        self,
        action: PendingAction,
        *,
        retryable: bool,
        code: str,
        message: str,
        remote_request_id: str = "",
    ) -> ExecutionResult:
        status = "failed_retryable" if retryable else "failed_final"
        await self.store.complete_pending_action(
            action.action_id,
            status=status,
            error_code=code,
            error_message=message,
            remote_request_id=remote_request_id,
        )
        await self.audit.record(
            self.action_type,
            status,
            tenant_key=action.tenant_key,
            app_id=action.app_id,
            principal_id=action.creator_subject_id,
            chat_id=action.chat_id,
            thread_id=action.thread_id,
            action_id=action.action_id,
            risk_level="high",
            payload_hash=action.payload_hash,
            metadata={"error_code": code},
        )
        if action.chat_id:
            retry_text = "可在待确认操作页重试。" if retryable else "请修正参数或重新授权。"
            await self.transport.send_markdown(
                action.chat_id,
                f"日程创建失败：{message} {retry_text}",
            )
        self.logger.error(
            "日历执行失败 action_id=%s status=%s code=%s request_id=%s error=%s",
            action.action_id,
            status,
            code,
            remote_request_id,
            message,
        )
        return ExecutionResult(
            status=status,
            error_code=code,
            error_message=message,
            remote_request_id=remote_request_id,
        )
