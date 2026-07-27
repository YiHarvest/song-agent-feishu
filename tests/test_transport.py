from __future__ import annotations

import asyncio
import logging
import threading

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
