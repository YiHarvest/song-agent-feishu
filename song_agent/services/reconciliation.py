"""远程成功已持久化记录的操作的本地完成处理。"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from ..models import DocumentBinding, PendingAction
from ..store import SqliteStore
from .audit import AuditService


class ActionReconciliationService:
    def __init__(self, store: SqliteStore, audit: AuditService) -> None:
        self.store = store
        self.audit = audit
        self.logger = logging.getLogger(__name__)

    async def reconcile(self, action: PendingAction) -> bool:
        """仅当远程资源 ID 证明成功时才完成本地状态。

        没有持久化提供方证据的未知操作保持未知状态，
        永远不会盲目重试。
        """

        if not action.remote_resource_id:
            return False
        try:
            if action.action_type.startswith("document."):
                await self._reconcile_document(action)
            elif action.action_type == "calendar.create":
                await self._reconcile_calendar(action)
            else:
                return False
            completed = await self.store.finish_reconciled_action(action.action_id)
            if completed:
                await self.audit.record(
                    "action.reconcile",
                    "success",
                    tenant_key=action.tenant_key,
                    app_id=action.app_id,
                    principal_id=action.creator_subject_id,
                    chat_id=action.chat_id,
                    thread_id=action.thread_id,
                    action_id=action.action_id,
                    payload_hash=action.payload_hash,
                    metadata={"remote_resource_id": action.remote_resource_id},
                )
            return completed
        except Exception:
            self.logger.exception("动作核对失败 action_id=%s", action.action_id)
            return False

    async def _reconcile_document(self, action: PendingAction) -> None:
        title = str(action.payload.get("title") or "Agent 云文档")
        token = action.remote_resource_id
        url = str(action.payload.get("document_url") or f"https://feishu.cn/docx/{token}")
        await self.store.save_document_binding(
            DocumentBinding(
                chat_id=action.chat_id,
                user_id=action.creator_subject_id,
                title=title,
                token=token,
                url=url,
            ),
            tenant_key=action.tenant_key,
            app_id=action.app_id,
            thread_id=action.thread_id,
        )

    async def _reconcile_calendar(self, action: PendingAction) -> None:
        record = await self.store.get_record_by_key(str(action.payload["record_key"]))
        if record is None or record.user_id != action.creator_subject_id:
            raise RuntimeError("日历动作对应的本地计划不存在")
        remote_ids = json.loads(action.remote_resource_id)
        if not isinstance(remote_ids, dict):
            raise RuntimeError("日历远端资源映射无效")
        for task in record.tasks:
            event_id = remote_ids.get(task.id)
            if isinstance(event_id, str) and event_id:
                task.calendar_event_id = event_id
        record.plan_status = "confirmed"
        record.updated_at = datetime.now(UTC).isoformat()
        await self.store.save_record(record)
