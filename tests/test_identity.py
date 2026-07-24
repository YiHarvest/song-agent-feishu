from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from song_agent.models import FeishuIdentity, IncomingMessage, UserTokenContext


def test_identity_prefers_union_then_tenant_user_then_open_id() -> None:
    assert (
        FeishuIdentity(
            app_id="app",
            open_id="open",
            user_id="tenant-user",
            union_id="union",
        ).subject_id
        == "union"
    )
    assert FeishuIdentity(app_id="app", open_id="open", user_id="tenant-user").subject_id == "tenant-user"
    assert FeishuIdentity(app_id="app", open_id="open").subject_id == "open"


def test_conversation_key_isolated_by_tenant_app_thread_and_subject() -> None:
    base = dict(
        message_id="message",
        tenant_key="tenant",
        app_id="app",
        user_id="union-a",
        open_id="open-a",
        union_id="union-a",
        chat_id="chat",
        chat_type="group",
        message_type="text",
        text="hello",
    )
    first = IncomingMessage(**base, thread_id="thread-a")
    second = IncomingMessage(**{**base, "message_id": "message-2"}, thread_id="thread-b")
    third = IncomingMessage(
        **{**base, "message_id": "message-3", "user_id": "union-b", "union_id": "union-b"},
        thread_id="thread-a",
    )
    assert first.conversation_key().serialize() != second.conversation_key().serialize()
    assert first.conversation_key().serialize() != third.conversation_key().serialize()
    assert first.chat_queue_key() == third.chat_queue_key()


def test_user_token_context_is_immutable() -> None:
    context = UserTokenContext(
        tenant_key="tenant",
        app_id="app",
        subject_id="union",
        open_id="open",
        access_token="access",
        expires_at=datetime.now(UTC),
        scopes=frozenset({"calendar:calendar"}),
    )
    with pytest.raises(FrozenInstanceError):
        context.access_token = "other"  # type: ignore[misc]
