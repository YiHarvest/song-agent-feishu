"""飞书任务写操作执行器。"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ValidationError

from ..domain.commands import TaskCreateCommand, TaskTargetCommand, TaskUpdateCommand
from ..domain.results import ExecutionContext, ExecutionResult
from ..feishu.oauth import FeishuOAuth
from ..feishu.openapi import FeishuApiError, FeishuOpenApi
from ..feishu.transport import FeishuTransport
from ..models import FeishuIdentity, PendingAction
from ..services.audit import AuditService
from ..store import SqliteStore


class _TaskActionExecutor:
    action_type: ClassVar[str]
    command_type: ClassVar[type[BaseModel]]

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
        except ValidationError as error:
            return await self._finish(
                pending_action,
                error,
                retryable=False,
                code="invalid_payload",
            )
        identity = FeishuIdentity(
            tenant_key=pending_action.tenant_key,
            app_id=pending_action.app_id,
            open_id=pending_action.creator_open_id,
            union_id=pending_action.creator_subject_id,
        )
        token = await self.oauth.get_valid_token_context(
            identity,
            ("task:task:write",),
        )
        if not token:
            return await self._finish(
                pending_action,
                RuntimeError("用户任务授权不可用"),
                retryable=False,
                code="authorization_required",
            )
        try:
            result = await self._call(command, token, pending_action)
        except FeishuApiError as error:
            return await self._finish(
                pending_action,
                error,
                retryable=error.retryable,
                code=str(error.code or "feishu_task_error"),
                request_id=error.request_id,
            )
        resource_id = str(
            result.get("task_guid")
            or pending_action.payload.get("task_guid")
            or ""
        )
        await self.store.complete_pending_action(
            pending_action.action_id,
            status="succeeded",
            result=result,
            remote_resource_id=resource_id,
            remote_request_id=str(result.get("request_id") or ""),
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
            metadata={"resource_type": "task", "resource_id": resource_id},
        )
        if pending_action.chat_id:
            await self.transport.send_markdown(
                pending_action.chat_id,
                f"✅ {self._success_message()}",
            )
        return ExecutionResult(status="succeeded", result=result)

    async def _call(
        self,
        command: BaseModel,
        token,
        action: PendingAction,
    ) -> dict:
        if self.action_type == "task.create":
            assert isinstance(command, TaskCreateCommand)
            return await self.openapi.create_task(
                command,
                token,
                idempotency_key=action.idempotency_key or action.payload_hash,
            )
        if self.action_type == "task.update":
            assert isinstance(command, TaskUpdateCommand)
            return await self.openapi.update_task(command, token)
        assert isinstance(command, TaskTargetCommand)
        if self.action_type == "task.complete":
            return await self.openapi.complete_task(command, token)
        return await self.openapi.delete_task(command, token)

    def _success_message(self) -> str:
        return {
            "task.create": "任务已创建。",
            "task.update": "任务已更新。",
            "task.complete": "任务已完成。",
            "task.delete": "任务已删除。",
        }[self.action_type]

    async def _finish(
        self,
        action: PendingAction,
        error: Exception,
        *,
        retryable: bool,
        code: str,
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


class TaskCreateExecutor(_TaskActionExecutor):
    action_type = "task.create"
    command_type = TaskCreateCommand


class TaskUpdateExecutor(_TaskActionExecutor):
    action_type = "task.update"
    command_type = TaskUpdateCommand


class TaskCompleteExecutor(_TaskActionExecutor):
    action_type = "task.complete"
    command_type = TaskTargetCommand


class TaskDeleteExecutor(_TaskActionExecutor):
    action_type = "task.delete"
    command_type = TaskTargetCommand
