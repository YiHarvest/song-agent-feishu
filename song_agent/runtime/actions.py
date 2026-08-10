from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime

from ..kernel.errors import ProviderError
from ..kernel.intent import UnknownResolutionPolicy
from ..kernel.models import ExecutionContext, IntentResult, PrincipalIdentity, RequestEnvelope
from ..ports.repositories import (
    ActionRecord,
    ActionRepository,
    ActionStatus,
    ExecutionRepository,
    ExecutionStatus,
)
from .catalog import IntentCatalog
from .dispatcher import IntentDispatcher

logger = logging.getLogger(__name__)


class ActionService:
    def __init__(
        self,
        actions: ActionRepository,
        dispatcher: IntentDispatcher,
        catalog: IntentCatalog,
        executions: ExecutionRepository,
    ) -> None:
        self.actions = actions
        self.dispatcher = dispatcher
        self.catalog = catalog
        self.executions = executions

    async def get(self, action_id: str, principal: PrincipalIdentity) -> ActionRecord | None:
        action = await self.actions.get(action_id)
        return action if action and _same_principal(action.requested_by, principal) else None

    async def confirm(self, action_id: str, principal: PrincipalIdentity) -> IntentResult:
        claimed = await self.actions.claim(
            action_id,
            confirmed_by=principal,
            now=datetime.now(UTC),
        )
        if claimed is None:
            return IntentResult.failure(
                code="action.not_confirmable",
                message="操作不存在、已过期、已处理或不属于当前用户。",
            )
        result = await self._execute_claimed(claimed, principal)
        status = _action_status_for_result(result)
        await self.actions.finish(action_id, status=status, result=result)
        return result

    async def cancel(self, action_id: str, principal: PrincipalIdentity) -> IntentResult:
        cancelled = await self.actions.cancel(
            action_id,
            principal=principal,
            now=datetime.now(UTC),
        )
        if cancelled is None:
            return IntentResult.failure(
                code="action.not_cancellable",
                message="操作不存在、已过期、已处理或不属于当前用户。",
            )
        return IntentResult.success(
            code="action.cancelled",
            data={"action_id": action_id},
            message="操作已取消。",
        )

    async def reconcile(self, action_id: str, principal: PrincipalIdentity) -> IntentResult:
        action = await self.get(action_id, principal)
        if action is None or action.status is not ActionStatus.UNKNOWN:
            return IntentResult.failure(
                code="action.not_reconcilable",
                message="该操作当前不能核对远端状态。",
            )
        validation_failure = self.dispatcher._validate_persisted_action(action, principal)
        if validation_failure is not None:
            return validation_failure
        spec = self.catalog.resolve(action.intent_id)
        if spec.reconcile is None or spec.unknown_resolution_policy is UnknownResolutionPolicy.NONE:
            return IntentResult.failure(
                code="action.reconcile_unsupported",
                message="该能力不支持自动核对远端状态。",
            )
        timeout = min(
            spec.execution_timeout_seconds,
            self.dispatcher.max_execution_timeout_seconds,
        )
        context, arguments = self._resolution_context(action, spec.version, timeout)
        try:
            async with asyncio.timeout(timeout):
                candidate = await spec.reconcile(
                    context, spec.arguments_model.model_validate(arguments)
                )
            if candidate is None:
                result = None
            else:
                validated = spec.result_model.model_validate(candidate)
                result = IntentResult.model_validate(validated.model_dump())
        except (ProviderError, TimeoutError):
            logger.info(
                "action reconciliation could not confirm remote state",
                extra={"action_id": action_id, "intent_id": action.intent_id},
            )
            result = None
        except Exception:
            logger.exception(
                "action reconciliation failed",
                extra={"action_id": action_id, "intent_id": action.intent_id},
            )
            result = None
        if result is None:
            return IntentResult(
                status="failure",
                code="action.still_unknown",
                data={"action_id": action_id},
                message="远端结果仍无法确认。",
            )
        status = _action_status_for_result(result)
        if status is not ActionStatus.UNKNOWN:
            await self.actions.resolve_unknown(action_id, status=status, result=result)
            await self.executions.resolve_unknown(
                f"exec_{action.action_id}",
                status=(
                    ExecutionStatus.SUCCEEDED
                    if status is ActionStatus.SUCCEEDED
                    else ExecutionStatus.FAILED
                ),
                result=result,
            )
        return result

    async def retry_unknown(self, action_id: str, principal: PrincipalIdentity) -> IntentResult:
        action = await self.get(action_id, principal)
        if action is None or action.status is not ActionStatus.UNKNOWN:
            return IntentResult.failure(
                code="action.not_retryable",
                message="该操作当前不能重试。",
            )
        spec = self.catalog.resolve(action.intent_id)
        if spec.unknown_resolution_policy not in {
            UnknownResolutionPolicy.RETRY_IDEMPOTENT,
            UnknownResolutionPolicy.RECONCILE_THEN_RETRY,
        }:
            return IntentResult.failure(
                code="action.retry_unsupported",
                message="该 Provider 不支持安全重试未知结果。",
            )
        if spec.unknown_resolution_policy is UnknownResolutionPolicy.RECONCILE_THEN_RETRY:
            reconciled = await self.reconcile(action_id, principal)
            refreshed = await self.get(action_id, principal)
            if refreshed is None or refreshed.status is not ActionStatus.UNKNOWN:
                return reconciled
        claimed = await self.actions.claim_unknown_for_retry(action_id, principal=principal)
        if claimed is None:
            return IntentResult.failure(
                code="action.retry_conflict",
                message="该操作正在处理或状态已经改变。",
            )
        result = await self._execute_claimed(claimed, principal)
        await self.actions.finish(
            action_id,
            status=_action_status_for_result(result),
            result=result,
        )
        return result

    async def _execute_claimed(
        self,
        action: ActionRecord,
        principal: PrincipalIdentity,
    ) -> IntentResult:
        try:
            return await self.dispatcher._execute_confirmed(action, confirmed_by=principal)
        except Exception:
            logger.exception(
                "confirmed action failed before reaching a handler",
                extra={"action_id": action.action_id},
            )
            return IntentResult.failure(
                code="action.confirmed_execution_failed",
                message="确认执行失败，且请求未送达远端。",
            )

    @staticmethod
    def _resolution_context(
        action: ActionRecord,
        intent_version: int,
        timeout: float,
    ) -> tuple[ExecutionContext, dict]:
        envelope = RequestEnvelope.model_validate(
            {
                "request_id": f"reconcile:{action.action_id}",
                "principal": action.principal,
                "delivery_target": action.delivery_target,
                "source": "action",
                "payload": {
                    "kind": "intent_execution",
                    "intent_id": action.intent_id,
                    "arguments": action.arguments,
                },
            }
        )
        context = ExecutionContext(
            execution_id=f"reconcile:{action.action_id}",
            envelope=envelope,
            intent_id=action.intent_id,
            intent_version=intent_version,
            idempotency_key=action.idempotency_key,
            deadline=time.monotonic() + timeout,
        )
        return context, action.arguments


def _same_principal(left: PrincipalIdentity, right: PrincipalIdentity) -> bool:
    return (
        left.tenant_id == right.tenant_id
        and left.principal_id == right.principal_id
        and left.auth_provider == right.auth_provider
    )


def _action_status_for_result(result: IntentResult) -> ActionStatus:
    if result.code.endswith("unknown") or result.data.get("delivery_state") in {"accepted", "unknown"}:
        return ActionStatus.UNKNOWN
    return ActionStatus.SUCCEEDED if result.status == "success" else ActionStatus.FAILED
