from __future__ import annotations

import asyncio
import logging
import threading
from types import SimpleNamespace

import pytest

from song_agent.feishu import transport
from song_agent.feishu.mcp import markdown_to_text_blocks
from song_agent.feishu.transport import clean_incoming_text, parse_message_text


def test_plain_text_and_mentions_are_parsed() -> None:
    raw = parse_message_text("text", '{"text":"@_user_1 /help"}')
    assert clean_incoming_text(raw) == "/help"


def test_post_content_is_flattened() -> None:
    raw = parse_message_text(
        "post",
        '{"zh_cn":{"content":[[{"tag":"text","text":"今天"},{"tag":"text","text":"安排"}]]}}',
    )
    assert raw == "今天\n安排"


def test_unsupported_media_is_empty() -> None:
    assert parse_message_text("audio", '{"file_key":"abc"}') == ""


def test_markdown_becomes_safe_docx_text_blocks() -> None:
    blocks = markdown_to_text_blocks("# 测试文档\n\n## 结果\n\n- 通过", "测试文档")
    assert [block["text"]["elements"][0]["text_run"]["content"] for block in blocks] == [
        "结果",
        "• 通过",
    ]


def test_websocket_client_uses_thread_owned_event_loop() -> None:
    captured: dict[str, asyncio.AbstractEventLoop] = {}

    class StubWsClient:
        def start(self) -> None:
            captured["running"] = asyncio.get_event_loop()
            captured["sdk"] = transport.lark_ws_client.loop

    thread = threading.Thread(
        target=transport._run_ws_client,
        args=(StubWsClient(),),  # type: ignore[arg-type]
    )
    thread.start()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert captured["running"] is captured["sdk"]
    assert captured["running"].is_closed()


@pytest.mark.asyncio
async def test_transport_close_drains_inflight_message_tasks() -> None:
    instance = object.__new__(transport.FeishuTransport)
    instance._accepting = True
    instance._futures = set()
    instance._futures_lock = threading.Lock()
    instance.logger = logging.getLogger("test.transport.close")
    release = asyncio.Event()

    async def handler() -> None:
        await release.wait()

    future = asyncio.run_coroutine_threadsafe(handler(), asyncio.get_running_loop())
    instance._futures.add(future)
    future.add_done_callback(instance._finish_future)

    closing = asyncio.create_task(instance.close(timeout_seconds=1))
    await asyncio.sleep(0)
    assert instance._accepting is False
    release.set()
    await closing
    assert not instance._futures


@pytest.mark.asyncio
async def test_new_group_is_auto_registered_for_any_sender() -> None:
    class Store:
        def __init__(self) -> None:
            self.added: list[tuple[str, str, str]] = []

        async def add_group_chat_id(self, chat_id, *, tenant_key, app_id) -> None:
            self.added.append((chat_id, tenant_key, app_id))

    instance = object.__new__(transport.FeishuTransport)
    instance.settings = SimpleNamespace(feishu_app_id="app")
    instance.store = Store()
    instance.logger = logging.getLogger("test.transport.group")
    instance.group_ids = {"oc_hermes"}
    received = []

    async def handler(message) -> None:
        received.append(message)

    event = SimpleNamespace(
        header=SimpleNamespace(tenant_key="tenant", event_id="event-new-group"),
        event=SimpleNamespace(
            sender=SimpleNamespace(
                sender_id=SimpleNamespace(
                    open_id="ou_regular_member",
                    user_id="user",
                    union_id="union",
                )
            ),
            message=SimpleNamespace(
                message_id="om_new_group",
                chat_id="oc_song_agent",
                chat_type="group",
                message_type="text",
                content='{"text":"@_user_1 你好"}',
                thread_id="",
                root_id="",
            ),
        ),
    )

    await instance._dispatch(event, handler)

    assert instance.group_ids == {"oc_hermes", "oc_song_agent"}
    assert instance.store.added == [("oc_song_agent", "tenant", "app")]
    assert len(received) == 1
    assert received[0].open_id == "ou_regular_member"
    assert received[0].chat_id == "oc_song_agent"


@pytest.mark.asyncio
async def test_registered_group_accepts_messages_from_different_members() -> None:
    class Store:
        async def add_group_chat_id(self, *args, **kwargs) -> None:
            raise AssertionError("registered group must not be inserted again")

    instance = object.__new__(transport.FeishuTransport)
    instance.settings = SimpleNamespace(feishu_app_id="app")
    instance.store = Store()
    instance.logger = logging.getLogger("test.transport.group")
    instance.group_ids = {"oc_song_agent"}
    received = []

    async def handler(message) -> None:
        received.append(message)

    def event(open_id: str, message_id: str):
        return SimpleNamespace(
            header=SimpleNamespace(tenant_key="tenant", event_id=f"event-{message_id}"),
            event=SimpleNamespace(
                sender=SimpleNamespace(
                    sender_id=SimpleNamespace(
                        open_id=open_id,
                        user_id="",
                        union_id="",
                    )
                ),
                message=SimpleNamespace(
                    message_id=message_id,
                    chat_id="oc_song_agent",
                    chat_type="group",
                    message_type="text",
                    content='{"text":"@_user_1 你好"}',
                    thread_id="",
                    root_id="",
                ),
            ),
        )

    await instance._dispatch(event("ou_member_1", "om_1"), handler)
    await instance._dispatch(event("ou_member_2", "om_2"), handler)

    assert [message.open_id for message in received] == [
        "ou_member_1",
        "ou_member_2",
    ]


@pytest.mark.asyncio
async def test_unmentioned_group_image_event_reaches_attachment_handler() -> None:
    class Store:
        async def add_group_chat_id(self, *args, **kwargs) -> None:
            raise AssertionError("registered group must not be inserted again")

    instance = object.__new__(transport.FeishuTransport)
    instance.settings = SimpleNamespace(feishu_app_id="app")
    instance.store = Store()
    instance.logger = logging.getLogger("test.transport.group.image")
    instance.group_ids = {"oc_song_agent"}
    received = []

    async def handler(message) -> None:
        received.append(message)

    event = SimpleNamespace(
        header=SimpleNamespace(tenant_key="tenant", event_id="event-image"),
        event=SimpleNamespace(
            sender=SimpleNamespace(
                sender_id=SimpleNamespace(
                    open_id="ou_member",
                    user_id="",
                    union_id="",
                )
            ),
            message=SimpleNamespace(
                message_id="om_image",
                chat_id="oc_song_agent",
                chat_type="group",
                message_type="image",
                content='{"image_key":"img_group_without_mention"}',
                thread_id="",
                root_id="",
            ),
        ),
    )

    await instance._dispatch(event, handler)

    assert len(received) == 1
    assert received[0].text == ""
    assert len(received[0].attachments) == 1
    assert received[0].attachments[0].kind == "image"
    assert received[0].attachments[0].resource_key == "img_group_without_mention"
