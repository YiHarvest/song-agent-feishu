"""飞书日程更新与删除执行器。"""

from __future__ import annotations

from ..domain.commands import CalendarDeleteCommand, CalendarUpdateCommand
from ..domain.results import ExecutionContext, ExecutionResult
from ..feishu.oauth import FeishuOAuth
from ..feishu.openapi import FeishuApiError, FeishuOpenApi
from ..feishu.transport import FeishuTransport
from ..models import FeishuIdentity, PendingAction
from ..services.audit import AuditService
from ..store import SqliteStore


class _CalendarMutationExecutor:
    action_type = ""
    command_type = CalendarUpdateCommand

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

    async def execute(
        self,
        pending_action: PendingAction,
        context: ExecutionContext,
    ) -> ExecutionResult:
        if not await self.store.claim_action_execution(
            pending_action.action_id,
            worker_id=context.worker_id,
        ):
            return ExecutionResult(
                status="failed_final",
                error_code="claim_failed",
                error_message="动作未处于可执行状态",
            )
        try:
            command = self.command_type.model_validate(pending_action.payload)
        except Exception as error:
            return await self._failure(pending_action, error, retryable=False)
        identity = FeishuIdentity(
            tenant_key=pending_action.tenant_key,
            app_id=pending_action.app_id,
            open_id=pending_action.creator_open_id,
            union_id=pending_action.creator_subject_id,
        )
        token = await self.oauth.get_valid_token_context(
            identity,
            ("calendar:calendar",),
        )
        if not token:
            return await self._failure(
                pending_action,
                RuntimeError("用户日历授权不可用"),
                retryable=False,
                code="authorization_required",
            )
        try:
            if self.action_type == "calendar.update":
                data = await self.openapi.update_calendar(command, token)
            else:
                data = await self.openapi.delete_calendar(command, token)
        except FeishuApiError as error:
            return await self._failure(
                pending_action,
                error,
                retryable=error.retryable,
                code=str(error.code or "feishu_calendar_error"),
                request_id=error.request_id,
            )
        resource_id = str(
            data.get("event_id")
            or pending_action.payload.get("event_id")
            or ""
        )
        await self.store.complete_pending_action(
            pending_action.action_id,
            status="succeeded",
            result=data,
            remote_resource_id=resource_id,
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
            metadata={"resource_id": resource_id},
        )
        if pending_action.chat_id:
            await self.transport.send_markdown(
                pending_action.chat_id,
                "✅ 日程已更新。"
                if self.action_type == "calendar.update"
                else "✅ 日程已删除。",
            )
        return ExecutionResult(status="succeeded", result=data)

    async def _failure(
        self,
        action: PendingAction,
        error: Exception,
        *,
        retryable: bool,
        code: str = "invalid_payload",
        request_id: str = "",
    ) -> ExecutionResult:
        status = "failed_retryable" if retryable else "failed_final"
        await self.store.complete_pending_action(
            action.action_id,
            status=status,
            error_code=code,
            error_message=str(error),
            remote_request_id=request_id,
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
        return ExecutionResult(
            status=status,
            error_code=code,
            error_message=str(error),
            remote_request_id=request_id,
        )


class CalendarUpdateExecutor(_CalendarMutationExecutor):
    action_type = "calendar.update"
    command_type = CalendarUpdateCommand


class CalendarDeleteExecutor(_CalendarMutationExecutor):
    action_type = "calendar.delete"
    command_type = CalendarDeleteCommand


class ReminderCancelExecutor(CalendarDeleteExecutor):
    action_type = "reminder.cancel"
