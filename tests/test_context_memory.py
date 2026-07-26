from pathlib import Path

import pytest

from song_agent.context.builders import AgentRuntimeContextBuilder, BusinessContextBuilder
from song_agent.context.models import ContextBudget
from song_agent.context.service import ConversationContextService
from song_agent.domain.intents import UserRequest
from song_agent.models import FeishuIdentity
from song_agent.services.encryption import AesGcmTokenCipher
from song_agent.store import SqliteStore


class SummaryLlm:
    calls = 0

    async def generate(self, schema, system, user, **kwargs):
        self.calls += 1
        return schema.model_validate(
            {
                "participants": ["张总"],
                "active_topics": ["融资"],
                "open_loops": [],
                "decisions": ["默认会议 60 分钟"],
                "memory_updates": [
                    {
                        "memory_type": "preference",
                        "memory_key": "default_event_duration",
                        "memory_value": "60",
                        "confidence": 0.95,
                    }
                ],
            }
        )


async def make_store(tmp_path: Path) -> SqliteStore:
    store = SqliteStore(
        tmp_path / "context.db",
        app_id="app",
        token_cipher=AesGcmTokenCipher({1: b"1" * 32}, 1),
    )
    await store.initialize()
    return store


def request(message_id: str, *, open_id: str = "open-a") -> UserRequest:
    return UserRequest(
        identity=FeishuIdentity(
            tenant_key="tenant",
            app_id="app",
            open_id=open_id,
            union_id=f"union-{open_id}",
        ),
        text=f"消息 {message_id}",
        source="feishu",
        chat_id="chat",
        thread_id="thread",
        message_id=message_id,
    )


@pytest.mark.asyncio
async def test_compaction_keeps_raw_messages_and_writes_summary_and_memory(
    tmp_path: Path,
) -> None:
    store = await make_store(tmp_path)
    try:
        builder = BusinessContextBuilder(store, timezone="Asia/Shanghai")
        llm = SummaryLlm()
        service = ConversationContextService(
            store,
            llm,
            builder,
            compact_after_messages=16,
            keep_recent_messages=8,
        )
        for index in range(8):
            item = request(f"m-{index}")
            await service.record_user(item)
            await service.record_assistant(item, f"答复 {index}")

        context = await builder.build_for_intent_extraction(request("current"))
        count = await (
            await store.db.execute(
                "SELECT COUNT(*) AS count FROM conversation_messages"
            )
        ).fetchone()
        summarized = await (
            await store.db.execute(
                """
                SELECT COUNT(*) AS count FROM conversation_messages
                WHERE summarized_at IS NOT NULL
                """
            )
        ).fetchone()

        assert count["count"] == 16
        assert summarized["count"] == 8
        assert len(context.recent_messages) == 8
        assert context.conversation_summary
        assert context.conversation_summary.active_topics == ["融资"]
        assert context.memories[0].memory_key == "default_event_duration"
        assert llm.calls == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_context_is_tenant_and_principal_scoped_and_has_six_layers(
    tmp_path: Path,
) -> None:
    store = await make_store(tmp_path)
    try:
        builder = BusinessContextBuilder(store, timezone="Asia/Shanghai")
        first = request("first")
        await ConversationContextService(store, SummaryLlm(), builder).record_user(first)

        own = await builder.build_for_intent_extraction(first)
        other = await builder.build_for_intent_extraction(
            request("other", open_id="open-b")
        )
        metadata = AgentRuntimeContextBuilder(
            ContextBudget(max_input_tokens=300, reserved_output_tokens=50)
        ).metadata(own)

        assert own.recent_messages
        assert other.recent_messages == []
        assert set(metadata) == {
            "request_context",
            "business_context",
            "conversation_context",
            "summary_context",
            "memory_context",
            "retrieved_context",
        }
        assert metadata["request_context"]["principal_id"] == "union-open-a"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_tool_result_reference_enforces_principal_scope(tmp_path: Path) -> None:
    store = await make_store(tmp_path)
    try:
        result_ref = await store.save_tool_result(
            tenant_key="tenant",
            app_id="app",
            principal_id="principal-a",
            tool_name="websearch.search",
            summary="2 results",
            payload={"items": [1, 2]},
            truncated=False,
        )
        own = await store.get_tool_result(
            result_ref,
            tenant_key="tenant",
            app_id="app",
            principal_id="principal-a",
        )
        other = await store.get_tool_result(
            result_ref,
            tenant_key="tenant",
            app_id="app",
            principal_id="principal-b",
        )
        assert own and own["payload"] == {"items": [1, 2]}
        assert other is None
    finally:
        await store.close()
