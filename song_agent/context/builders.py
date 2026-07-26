"""确定性业务与开放 Agent 使用不同的 Context Builder。"""

from __future__ import annotations

import hashlib
from datetime import datetime
from zoneinfo import ZoneInfo

from ..domain.intents import UserRequest
from ..store import SqliteStore
from .models import BusinessContext, ContextBudget, RequestContext


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


class AgentRuntimeContextBuilder:
    """按预算把六层上下文压成开放 Agent 的运行时元数据。"""

    def __init__(self, budget: ContextBudget | None = None) -> None:
        self.budget = budget or ContextBudget()

    def metadata(self, context: BusinessContext) -> dict:
        recent = [
            {"role": message.role, "content": message.content}
            for message in context.recent_messages
        ]
        summary = (
            context.conversation_summary.model_dump(mode="json")
            if context.conversation_summary
            else {}
        )
        memories = [memory.model_dump(mode="json") for memory in context.memories]
        sections = {
            "request_context": context.request.model_dump(mode="json"),
            "business_context": {
                "active_pending_action": context.active_pending_action,
            },
            "conversation_context": recent,
            "summary_context": summary,
            "memory_context": memories,
            "retrieved_context": context.retrieved,
        }
        return _fit_budget(sections, self.budget.available_tokens)


def _fit_budget(sections: dict, max_tokens: int) -> dict:
    """保留必需层，按低优先级裁剪可选列表。"""

    def estimate(value: object) -> int:
        return max(1, len(str(value)) // 3)

    while estimate(sections) > max_tokens:
        if sections["retrieved_context"]:
            sections["retrieved_context"] = {}
        elif sections["memory_context"]:
            sections["memory_context"].pop()
        elif len(sections["conversation_context"]) > 4:
            sections["conversation_context"].pop(0)
        else:
            break
    return sections
