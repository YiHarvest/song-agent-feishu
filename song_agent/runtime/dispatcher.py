from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ValidationError

from ..kernel.errors import CatalogError, DeliveryState, ProviderError
from ..kernel.intent import IntentSpec
from ..kernel.models import (
    ExecutionContext,
    IntentExecutionPayload,
    IntentResult,
    PrincipalIdentity,
    RequestEnvelope,
)
from ..ports.auth import AuthorizationPort
from ..ports.repositories import (
    ActionRecord,
    ActionRepository,
    ActionStatus,
    AuditRepository,
    ExecutionRepository,
    ExecutionStatus,
)
from .catalog import IntentCatalog
from .context import ContextAssembler
from .risk import RiskPolicy

logger = logging.getLogger(__name__)


class IntentDispatcher:
    """The sole execution kernel for already-known intent IDs."""

    def __init__(
        self,
        *,
        catalog: IntentCatalog,
        authorization: AuthorizationPort,
        executions: ExecutionRepository,
        actions: ActionRepository,
        audit: AuditRepository,
        contexts: ContextAssembler,
        risk_policy: RiskPolicy,
        module_versions: dict[str, str],
        max_execution_timeout_seconds: float = 180.0,
    ) -> None:
        self.catalog = catalog
        self.authorization = authorization
        self.executions = executions
        self.actions = actions
        self.audit = audit
        self.contexts = contexts
        self.risk_policy = risk_policy
        self.module_versions = dict(module_versions)
        self.max_execution_timeout_seconds = max_execution_timeout_seconds

    async def execute(self, envelope: RequestEnvelope) -> IntentResult:
        if not isinstance(envelope.payload, IntentExecutionPayload):
            return IntentResult.failure(
                code="dispatcher.invalid_payload",
                message="IntentDispatcher requires a known intent payload.",
            )
        try:
            spec = self.catalog.resolve(envelope.payload.intent_id)
        except (CatalogError, ValueError) as error:
            return IntentResult.failure(
                code="dispatcher.intent_not_found",
                message=str(error),
            )
        try:
            arguments = spec.arguments_model.model_validate(envelope.payload.arguments)
        except ValidationError as error:
            return IntentResult(
                status="clarification_required",
                code="dispatcher.invalid_arguments",
                data={"errors": error.errors(include_url=False)},
                message="请求参数不完整或格式不正确。",
            )
        return await self._execute(spec, arguments, envelope, confirmed_action=None)

    async def _execute_confirmed(
        self,
        action: ActionRecord,
        *,
        confirmed_by: PrincipalIdentity,
    ) -> IntentResult:
        """Kernel-only confirmed path. External adapters receive only ActionService."""
        validation_failure = self._validate_persisted_action(action, confirmed_by)
        if validation_failure is not None:
            return validation_failure
        spec = self.catalog.resolve(action.intent_id)
        arguments = spec.arguments_model.model_validate(action.arguments)
        envelope = RequestEnvelope(
            request_id=f"action:{action.action_id}",
            principal=action.principal,
            delivery_target=action.delivery_target,
            conversation=None,
            source="action",
            payload=IntentExecutionPayload(
                intent_id=action.intent_id,
                arguments=action.arguments,
            ),
            metadata={"action_id": action.action_id},
        )
        return await self._execute(spec, arguments, envelope, confirmed_action=action)

    def _validate_persisted_action(
        self,
        action: ActionRecord,
        confirmed_by: PrincipalIdentity,
    ) -> IntentResult | None:
        if (
            confirmed_by.tenant_id != action.requested_by.tenant_id
            or confirmed_by.principal_id != action.requested_by.principal_id
            or confirmed_by.auth_provider != action.requested_by.auth_provider
            or action.confirmed_by != confirmed_by
        ):
            return IntentResult.failure(
                code="action.confirming_principal_mismatch",
                message="确认身份与操作申请身份不一致。",
            )
        spec = self.catalog.resolve(action.intent_id)
        if action.intent_version != spec.version:
            return IntentResult.failure(
                code="action.intent_version_incompatible",
                message="该确认操作来自不兼容的旧版本，请重新发起。",
            )
        if action.module_version != self.module_versions.get(spec.module_id):
            return IntentResult.failure(
                code="action.module_version_incompatible",
                message="模块版本已经变化，请重新发起操作。",
            )
        expected_hash = canonical_payload_hash(
            intent_id=action.intent_id,
            intent_version=action.intent_version,
            arguments=action.arguments,
            principal=action.principal.model_dump(mode="json"),
            delivery_target=action.delivery_target.model_dump(mode="json"),
            module_id=action.module_id,
            module_version=action.module_version,
            permissions=action.permissions,
        )
        if expected_hash != action.payload_hash:
            return IntentResult.failure(
                code="action.integrity_check_failed",
                message="操作内容完整性校验失败。",
            )
        return None

    async def _execute(
        self,
        spec: IntentSpec,
        arguments: BaseModel,
        envelope: RequestEnvelope,
        *,
        confirmed_action: ActionRecord | None,
    ) -> IntentResult:
        permissions = spec.required_permissions
        if spec.permission_resolver is not None:
            permissions = permissions | spec.permission_resolver(arguments)
        authorization = await self.authorization.authorize(
            envelope.principal,
            permissions,
            envelope.delivery_target,
        )
        if not authorization.granted:
            return IntentResult(
                status="authorization_required",
                code=authorization.reason_code or "authorization.required",
                data={"authorization_url": authorization.authorization_url},
                message="执行该操作需要先完成授权。",
            )

        arguments_json = arguments.model_dump(mode="json")
        payload_hash = canonical_payload_hash(
            intent_id=spec.intent_id,
            intent_version=spec.version,
            arguments=arguments_json,
            principal=envelope.principal.model_dump(mode="json"),
            delivery_target=envelope.delivery_target.model_dump(mode="json"),
            module_id=spec.module_id,
            module_version=self.module_versions[spec.module_id],
            permissions=permissions,
        )
        if confirmed_action is not None and payload_hash != confirmed_action.payload_hash:
            return IntentResult.failure(
                code="action.execution_semantics_changed",
                message="操作的权限或执行语义已经变化，请重新发起。",
            )
        idempotency_key = (
            confirmed_action.idempotency_key
            if confirmed_action is not None
            else build_idempotency_key(envelope, spec, payload_hash)
        )

        if confirmed_action is None and self.risk_policy.requires_confirmation(spec):
            return await self._create_action(
                spec,
                arguments_json,
                envelope,
                permissions,
                payload_hash,
                idempotency_key,
            )

        execution_id = (
            f"exec_{confirmed_action.action_id}"
            if confirmed_action is not None
            else f"exec_{uuid.uuid4().hex}"
        )
        if spec.is_write:
            if confirmed_action is not None and confirmed_action.result is not None:
                retried = await self.executions.retry_unknown(execution_id)
                if retried is None:
                    return IntentResult.failure(
                        code="action.retry_execution_conflict",
                        message="原执行记录不处于可安全重试的未知状态。",
                    )
                reservation = None
            else:
                reservation = await self.executions.reserve(
                    execution_id=execution_id,
                    tenant_id=envelope.principal.tenant_id,
                    principal_id=envelope.principal.principal_id,
                    intent_id=spec.intent_id,
                    intent_version=spec.version,
                    idempotency_key=idempotency_key,
                    payload_hash=payload_hash,
                )
            if reservation is None:
                pass
            elif reservation.outcome == "replay" and reservation.record.result is not None:
                return reservation.record.result
            elif reservation.outcome == "conflict":
                return IntentResult.failure(
                    code="execution.idempotency_conflict",
                    message="该幂等键已经用于不同的请求。",
                )
            elif reservation.outcome == "in_progress":
                return IntentResult(
                    status="in_progress",
                    code="execution.in_progress",
                    data={"execution_id": reservation.record.execution_id},
                    message="相同请求正在执行中。",
                )
            else:
                execution_id = reservation.record.execution_id

        trace_id = str(envelope.metadata.get("trace_id") or uuid.uuid4().hex)
        await self._audit(
            trace_id=trace_id,
            envelope=envelope,
            spec=spec,
            phase="started",
            outcome="executing",
            payload_hash=payload_hash,
            metadata={"execution_id": execution_id},
        )
        assembled = await self.contexts.for_intent(envelope, spec)
        timeout = min(spec.execution_timeout_seconds, self.max_execution_timeout_seconds)
        execution_context = ExecutionContext(
            execution_id=execution_id,
            envelope=envelope,
            intent_id=spec.intent_id,
            intent_version=spec.version,
            idempotency_key=idempotency_key,
            deadline=time.monotonic() + timeout,
            contexts=assembled,
            permissions=authorization.granted_permissions,
        )
        execution_status = ExecutionStatus.SUCCEEDED
        provider_request_id = ""
        try:
            async with asyncio.timeout(timeout):
                handler_result = await spec.handler(execution_context, arguments)
                validated_result = spec.result_model.model_validate(handler_result)
                result = IntentResult.model_validate(validated_result.model_dump())
            if result.status == "failure":
                execution_status = ExecutionStatus.FAILED
        except ProviderError as error:
            provider_request_id = error.provider_request_id
            execution_status = (
                ExecutionStatus.UNKNOWN
                if spec.is_write
                and error.delivery_state in {DeliveryState.ACCEPTED, DeliveryState.UNKNOWN}
                else ExecutionStatus.FAILED
            )
            result = IntentResult.failure(
                code=error.error_code,
                message=error.safe_message,
                data={"delivery_state": error.delivery_state.value},
            )
        except TimeoutError:
            execution_status = ExecutionStatus.UNKNOWN if spec.is_write else ExecutionStatus.FAILED
            result = IntentResult.failure(
                code="execution.timeout_unknown" if spec.is_write else "execution.timeout",
                message=(
                    "操作超时，远端结果暂时无法确认。"
                    if spec.is_write
                    else "操作超时，请稍后重试。"
                ),
            )
        except Exception:
            logger.exception(
                "unexpected intent handler failure",
                extra={"intent_id": spec.intent_id, "execution_id": execution_id},
            )
            execution_status = (
                ExecutionStatus.UNKNOWN if spec.is_write else ExecutionStatus.FAILED
            )
            result = IntentResult.failure(
                code=(
                    "execution.internal_error_unknown"
                    if spec.is_write
                    else "execution.internal_error"
                ),
                message=(
                    "执行发生异常，远端结果暂时无法确认。"
                    if spec.is_write
                    else "执行失败，详细原因已记录。"
                ),
            )

        if spec.is_write:
            await self.executions.finish(
                execution_id,
                status=execution_status,
                result=result,
                provider_request_id=provider_request_id,
            )
        await self._audit(
            trace_id=trace_id,
            envelope=envelope,
            spec=spec,
            phase="finished",
            outcome=execution_status.value,
            payload_hash=payload_hash,
            metadata={
                "execution_id": execution_id,
                "result_code": result.code,
                "provider_request_id": provider_request_id,
            },
        )
        return result

    async def _create_action(
        self,
        spec: IntentSpec,
        arguments: dict[str, Any],
        envelope: RequestEnvelope,
        permissions: frozenset[str],
        payload_hash: str,
        idempotency_key: str,
    ) -> IntentResult:
        now = datetime.now(UTC)
        ttl = max(60, min(spec.confirmation_policy.ttl_seconds, 86400))
        action = ActionRecord(
            action_id=f"action_{uuid.uuid4().hex}",
            intent_id=spec.intent_id,
            intent_version=spec.version,
            module_id=spec.module_id,
            module_version=self.module_versions[spec.module_id],
            arguments=arguments,
            principal=envelope.principal,
            delivery_target=envelope.delivery_target,
            requested_by=envelope.principal,
            confirmed_by=None,
            risk_level=self.risk_policy.effective_risk(spec),
            permissions=permissions,
            payload_hash=payload_hash,
            idempotency_key=idempotency_key,
            status=ActionStatus.PENDING,
            result=None,
            expires_at=now + timedelta(seconds=ttl),
            created_at=now,
            updated_at=now,
        )
        stored = await self.actions.create(action)
        return IntentResult(
            status="confirmation_required",
            code="action.confirmation_required",
            data={
                "action_id": stored.action_id,
                "intent_id": stored.intent_id,
                "risk_level": stored.risk_level.value,
                "expires_at": stored.expires_at.isoformat(),
                "arguments": stored.arguments,
            },
            message="该操作需要确认后执行。",
            presentation_hints={"kind": "confirmation", "title": "确认操作"},
        )

    async def _audit(
        self,
        *,
        trace_id: str,
        envelope: RequestEnvelope,
        spec: IntentSpec,
        phase: str,
        outcome: str,
        payload_hash: str,
        metadata: dict[str, Any],
    ) -> None:
        await self.audit.append(
            trace_id=trace_id,
            request_id=envelope.request_id,
            tenant_id=envelope.principal.tenant_id,
            principal_id=envelope.principal.principal_id,
            channel=envelope.delivery_target.channel,
            intent_id=spec.intent_id,
            intent_version=spec.version,
            module_id=spec.module_id,
            phase=phase,
            outcome=outcome,
            payload_hash=payload_hash,
            metadata=metadata,
        )


def canonical_payload_hash(
    *,
    intent_id: str,
    intent_version: int,
    arguments: dict[str, Any],
    principal: dict[str, Any],
    delivery_target: dict[str, Any],
    module_id: str,
    module_version: str,
    permissions: frozenset[str],
) -> str:
    canonical = json.dumps(
        {
            "intent_id": intent_id,
            "intent_version": intent_version,
            "arguments": arguments,
            "principal": principal,
            "delivery_target": delivery_target,
            "module_id": module_id,
            "module_version": module_version,
            "permissions": sorted(permissions),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def build_idempotency_key(
    envelope: RequestEnvelope,
    spec: IntentSpec,
    payload_hash: str,
) -> str:
    supplied = str(envelope.metadata.get("idempotency_key") or "").strip()
    if supplied:
        return supplied[:255]
    material = ":".join(
        (
            envelope.principal.tenant_id,
            envelope.principal.principal_id,
            spec.intent_id,
            envelope.request_id,
            payload_hash,
        )
    )
    return hashlib.sha256(material.encode()).hexdigest()
