"""确定性业务与开放 Agent 使用不同的 Context Builder。"""

from __future__ import annotations

import hashlib
from datetime import datetime
from zoneinfo import ZoneInfo

from ..domain.intents import UserRequest
from ..store import SqliteStore
from .models import BusinessContext, RequestContext


class BusinessContextBuilder:
    def __init__(
        self,
        store: SqliteStore,
        *,
        timezone: str,
        recent_limit: int = 8,
    ) -> None:
        self.store = store
        self.timezone = timezone
        self.recent_limit = recent_limit

    async def build_for_intent_extraction(
        self,
        request: UserRequest,
    ) -> BusinessContext:
        context = self.request_context(request)
        return BusinessContext(
            request=context,
            recent_messages=await self.store.list_recent_conversation_messages(
                context.session_id,
                tenant_key=context.tenant_key,
                app_id=context.app_id,
                principal_id=context.principal_id,
                limit=self.recent_limit,
            ),
            conversation_summary=await self.store.get_conversation_summary(
                context.session_id,
                tenant_key=context.tenant_key,
                app_id=context.app_id,
                principal_id=context.principal_id,
            ),
            memories=await self.store.list_user_memories(
                tenant_key=context.tenant_key,
                app_id=context.app_id,
                principal_id=context.principal_id,
                limit=20,
            ),
            active_pending_action=await self.store.find_active_pending_action(
                tenant_key=context.tenant_key,
                app_id=context.app_id,
                principal_id=context.principal_id,
                thread_id=context.thread_id,
            ),
        )

    def request_context(self, request: UserRequest) -> RequestContext:
        identity = request.identity
        principal = identity.subject_id
        session_raw = ":".join(
            (
                identity.tenant_key,
                identity.app_id,
                request.chat_id,
                request.thread_id,
                principal,
            )
        )
        return RequestContext(
            request_id=request.event_id or request.message_id,
            tenant_key=identity.tenant_key,
            app_id=identity.app_id,
            principal_id=principal,
            channel=request.source,
            chat_id=request.chat_id,
            thread_id=request.thread_id,
            message_id=request.message_id,
            timezone=self.timezone,
            current_time=datetime.now(ZoneInfo(self.timezone)),
            session_id=hashlib.sha256(session_raw.encode()).hexdigest(),
        )
