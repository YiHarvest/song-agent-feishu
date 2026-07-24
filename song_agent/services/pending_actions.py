"""持久化敏感操作草稿的创建和验证。"""

from __future__ import annotations

import hashlib
import json
import time
import uuid

from ..models import DailyRecord, IncomingMessage, PendingAction
from ..store import SqliteStore


class PendingActionService:
    def __init__(self, store: SqliteStore, *, ttl_seconds: int = 1800) -> None:
        self.store = store
        self.ttl_seconds = ttl_seconds

    async def create_calendar_action(
        self,
        message: IncomingMessage,
        record: DailyRecord,
        task_ids: set[str],
    ) -> PendingAction:
        payload = {
            "record_key": record.key,
            "record_updated_at": record.updated_at,
            "task_ids": sorted(task_ids),
        }
        now = int(time.time())
        action = PendingAction(
            action_id=str(uuid.uuid4()),
            tenant_key=message.tenant_key,
            app_id=message.app_id,
            chat_id=message.chat_id,
            thread_id=message.thread_id or message.root_id,
            creator_subject_id=message.user_id,
            creator_open_id=message.open_id or message.user_id,
            action_type="calendar.create",
            payload=payload,
            payload_hash=payload_hash(payload),
            source_message_id=message.message_id,
            expires_at=now + self.ttl_seconds,
            created_at=now,
        )
        await self.store.save_pending_action(action)
        return action

    async def create_document_action(
        self,
        message: IncomingMessage,
        *,
        action_type: str,
        title: str,
        markdown: str,
        document_token: str = "",
        document_url: str = "",
    ) -> PendingAction:
        if action_type not in {"document.create", "document.append"}:
            raise ValueError("unsupported document action")
        payload = {
            "title": title,
            "markdown": markdown,
            "document_token": document_token,
            "document_url": document_url,
        }
        now = int(time.time())
        action = PendingAction(
            action_id=str(uuid.uuid4()),
            tenant_key=message.tenant_key,
            app_id=message.app_id,
            chat_id=message.chat_id,
            thread_id=message.thread_id or message.root_id,
            creator_subject_id=message.user_id,
            creator_open_id=message.open_id or message.user_id,
            action_type=action_type,
            payload=payload,
            payload_hash=payload_hash(payload),
            source_message_id=message.message_id,
            expires_at=now + self.ttl_seconds,
            created_at=now,
        )
        await self.store.save_pending_action(action)
        return action


def payload_hash(payload: dict) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
