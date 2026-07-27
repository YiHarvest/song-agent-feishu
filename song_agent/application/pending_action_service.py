"""飞书卡片使用的待确认动作服务。"""

from __future__ import annotations

import time
from collections.abc import Callable

from ..domain.results import ApplicationResult
from ..models import FeishuIdentity, PendingAction
from ..services.audit import AuditService
from ..store import SqliteStore


class PendingActionApplicationService:
    def __init__(
        self,
        store: SqliteStore,
        audit: AuditService,
        notify_outbox: Callable[[], None] | None = None,
    ) -> None:
        self.store = store
        self.audit = audit
        self.notify_outbox = notify_outbox or (lambda: None)

    def set_outbox_notifier(self, notifier: Callable[[], None]) -> None:
        self.notify_outbox = notifier

    async def confirm(
        self,
        identity: FeishuIdentity,
        action_id: str,
        *,
        event_id: str = "",
    ) -> ApplicationResult:
        if event_id and not await self.store.claim_event(
            event_id,
            "card.action.trigger",
            tenant_key=identity.tenant_key,
            app_id=identity.app_id,
        ):
            action = await self.store.get_pending_action(action_id)
            return ApplicationResult(
                status="ok",
                intent="pending_action.confirm",
                action_id=action_id,
                message="该卡片操作已处理。",
                data={"action_status": action.status if action else "missing"},
            )
        action = await self._owned_action(identity, action_id)
        if not action:
            return ApplicationResult(
                status="error",
                intent="pending_action.confirm",
                action_id=action_id,
                message="待确认操作不存在或不属于当前用户。",
            )
        if action.expires_at <= int(time.time()):
            await self.store.expire_pending_action(action_id)
            return ApplicationResult(
                status="error",
                intent="pending_action.confirm",
                action_id=action_id,
                message="待确认操作已过期。",
            )
        claimed = await self.store.claim_pending_action(
            action_id,
            actor_open_id=identity.open_id,
            payload_hash=action.payload_hash,
        )
        if not claimed:
            latest = await self.store.get_pending_action(action_id)
            return ApplicationResult(
                status="ok" if latest and latest.status != "awaiting_confirmation" else "error",
                intent="pending_action.confirm",
                action_id=action_id,
                message="操作已处理。" if latest else "无法确认操作。",
                data={"action_status": latest.status if latest else "missing"},
            )
        await self.audit.record(
            "pending_action.confirmation",
            "confirmed",
            tenant_key=action.tenant_key,
            app_id=action.app_id,
            principal_id=action.creator_subject_id,
            chat_id=action.chat_id,
            thread_id=action.thread_id,
            action_id=action.action_id,
            decision="confirm",
            risk_level="high",
            payload_hash=action.payload_hash,
        )
        self.notify_outbox()
        return ApplicationResult(
            status="ok",
            intent="pending_action.confirm",
            action_id=action_id,
            message="已确认，正在执行。",
            data={"action_status": "confirmed"},
        )

    async def cancel(
        self,
        identity: FeishuIdentity,
        action_id: str,
        *,
        event_id: str = "",
    ) -> ApplicationResult:
        if event_id and not await self.store.claim_event(
            event_id,
            "card.action.trigger",
            tenant_key=identity.tenant_key,
            app_id=identity.app_id,
        ):
            return ApplicationResult(
                status="ok",
                intent="pending_action.cancel",
                action_id=action_id,
                message="该卡片操作已处理。",
            )
        action = await self._owned_action(identity, action_id)
        if not action:
            return ApplicationResult(
                status="error",
                intent="pending_action.cancel",
                action_id=action_id,
                message="待确认操作不存在或不属于当前用户。",
            )
        cancelled = await self.store.cancel_pending_action(
            action_id,
            actor_open_id=identity.open_id,
            payload_hash=action.payload_hash,
        )
        return ApplicationResult(
            status="ok" if cancelled else "error",
            intent="pending_action.cancel",
            action_id=action_id,
            message="已取消。" if cancelled else "操作已处理，无法取消。",
        )

    async def retry(
        self,
        identity: FeishuIdentity,
        action_id: str,
    ) -> ApplicationResult:
        action = await self._owned_action(identity, action_id)
        if not action:
            return ApplicationResult(
                status="error",
                intent="pending_action.retry",
                action_id=action_id,
                message="操作不存在或不属于当前用户。",
            )
        retried = await self.store.retry_pending_action(
            action_id,
            tenant_key=identity.tenant_key,
            app_id=identity.app_id,
            principal_id=identity.subject_id,
        )
        if retried:
            self.notify_outbox()
        return ApplicationResult(
            status="ok" if retried else "error",
            intent="pending_action.retry",
            action_id=action_id,
            message="已重新入队。" if retried else "仅可重试 FAILED_RETRYABLE 操作。",
        )

    async def list(self, identity: FeishuIdentity) -> list[PendingAction]:
        return await self.store.list_pending_actions(
            tenant_key=identity.tenant_key,
            app_id=identity.app_id,
            principal_id=identity.subject_id,
        )

    async def get(
        self,
        identity: FeishuIdentity,
        action_id: str,
    ) -> PendingAction | None:
        return await self._owned_action(identity, action_id)

    async def _owned_action(
        self,
        identity: FeishuIdentity,
        action_id: str,
    ) -> PendingAction | None:
        action = await self.store.get_pending_action(action_id)
        if not action:
            return None
        if (
            action.tenant_key != identity.tenant_key
            or action.app_id != identity.app_id
            or action.creator_subject_id != identity.subject_id
            or action.creator_open_id != identity.open_id
        ):
            return None
        return action
