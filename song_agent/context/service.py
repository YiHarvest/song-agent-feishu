"""会话持久化、结构化摘要和长期记忆写入。"""

from __future__ import annotations

import logging
import uuid

from ..domain.intents import UserRequest
from ..llm import StructuredLlm
from ..store import SqliteStore
from .builders import BusinessContextBuilder
from .models import ConversationSummary

SUMMARY_PROMPT = """你是会话压缩器。只提取明确事实，不猜测。
输出结构化摘要：participants、active_topics、open_loops、decisions、memory_updates。
memory_updates 只包含稳定偏好、称呼、默认时区、默认时长和长期约束；
不得写入 PendingAction、message_id、API 返回值、临时错误或工具状态。"""


class ConversationContextService:
    def __init__(
        self,
        store: SqliteStore,
        llm: StructuredLlm,
        builder: BusinessContextBuilder,
        *,
        compact_after_messages: int = 16,
        keep_recent_messages: int = 8,
    ) -> None:
        self.store = store
        self.llm = llm
        self.builder = builder
        self.compact_after_messages = compact_after_messages
        self.keep_recent_messages = keep_recent_messages
        self.logger = logging.getLogger(__name__)

    async def record_user(self, request: UserRequest) -> None:
        context = self.builder.request_context(request)
        await self.store.append_conversation_message(
            session_id=context.session_id,
            message_id=request.message_id or request.event_id or str(uuid.uuid4()),
            role="user",
            content=request.text,
            tenant_key=context.tenant_key,
            app_id=context.app_id,
            principal_id=context.principal_id,
            chat_id=context.chat_id,
            thread_id=context.thread_id,
        )

    async def record_assistant(self, request: UserRequest, content: str) -> None:
        context = self.builder.request_context(request)
        await self.store.append_conversation_message(
            session_id=context.session_id,
            message_id=f"assistant:{request.message_id or request.event_id or uuid.uuid4()}",
            role="assistant",
            content=content,
            tenant_key=context.tenant_key,
            app_id=context.app_id,
            principal_id=context.principal_id,
            chat_id=context.chat_id,
            thread_id=context.thread_id,
        )
        try:
            await self.compact_if_needed(request)
        except Exception:
            self.logger.exception(
                "会话摘要失败，原始消息已保留 session=%s",
                context.session_id,
            )

    async def compact_if_needed(self, request: UserRequest) -> bool:
        context = self.builder.request_context(request)
        messages = await self.store.list_conversation_messages_for_compaction(
            context.session_id,
            tenant_key=context.tenant_key,
            app_id=context.app_id,
            principal_id=context.principal_id,
            keep_recent=self.keep_recent_messages,
            threshold=self.compact_after_messages,
        )
        if not messages:
            return False
        previous = await self.store.get_conversation_summary(
            context.session_id,
            tenant_key=context.tenant_key,
            app_id=context.app_id,
            principal_id=context.principal_id,
        )
        transcript = "\n".join(
            f"{message.role}: {message.content}" for message in messages
        )
        prompt = (
            f"已有摘要：{previous.model_dump_json() if previous else '{}'}\n"
            f"待压缩消息：\n{transcript}"
        )
        summary = await self.llm.generate(
            ConversationSummary,
            SUMMARY_PROMPT,
            prompt,
            run_id=f"summary:{context.session_id[:12]}",
            max_tokens=1800,
        )
        await self.store.save_conversation_summary(
            context.session_id,
            summary,
            covered_message_ids=[message.message_id for message in messages],
            tenant_key=context.tenant_key,
            app_id=context.app_id,
            principal_id=context.principal_id,
        )
        for memory in summary.memory_updates:
            await self.store.upsert_user_memory(
                memory,
                tenant_key=context.tenant_key,
                app_id=context.app_id,
                principal_id=context.principal_id,
            )
        return True
