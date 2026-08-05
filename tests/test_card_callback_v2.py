import asyncio
import time

import pytest
from lark_oapi.core.const import (
    LARK_REQUEST_NONCE,
    LARK_REQUEST_SIGNATURE,
    LARK_REQUEST_TIMESTAMP,
)
from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTrigger

from song_agent.app import _lark_sdk_headers
from song_agent.channels.feishu.card_handler import FeishuCardHandler
from song_agent.domain.results import ApplicationResult


class Service:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def confirm(self, identity, action_id, *, event_id=""):
        self.calls.append((event_id, action_id))
        return ApplicationResult(status="ok", message="已确认，正在执行。")

    async def cancel(self, identity, action_id, *, event_id=""):
        self.calls.append((event_id, action_id))
        return ApplicationResult(status="ok", message="已取消。")

    async def retry(self, identity, action_id, *, event_id=""):
        self.calls.append((event_id, action_id))
        return ApplicationResult(status="ok", message="已重新入队。")


def callback() -> P2CardActionTrigger:
    return P2CardActionTrigger(
        {
            "schema": "2.0",
            "header": {
                "event_id": "event-1",
                "event_type": "card.action.trigger",
                "tenant_key": "tenant",
                "app_id": "app",
            },
            "event": {
                "operator": {"open_id": "open-a", "union_id": "subject-a"},
                "action": {
                    "value": {
                        "action": "pending_action.confirm",
                        "action_id": "action-1",
                    }
                },
                "context": {"open_message_id": "message-1", "open_chat_id": "chat"},
            },
        }
    )


def test_lark_sdk_headers_restore_names_from_asgi_lowercase_headers() -> None:
    headers = _lark_sdk_headers(
        {
            "x-lark-request-timestamp": "timestamp",
            "x-lark-request-nonce": "nonce",
            "x-lark-signature": "signature",
        }
    )

    assert headers[LARK_REQUEST_TIMESTAMP] == "timestamp"
    assert headers[LARK_REQUEST_NONCE] == "nonce"
    assert headers[LARK_REQUEST_SIGNATURE] == "signature"


@pytest.mark.asyncio
async def test_v2_callback_uses_sdk_object_value_and_returns_toast_immediately() -> None:
    service = Service()
    handler = FeishuCardHandler(service, asyncio.get_running_loop())
    started = time.perf_counter()
    response = await asyncio.to_thread(handler.handle, callback())
    assert time.perf_counter() - started < 1
    assert service.calls == [("event-1", "action-1")]
    assert response.toast.type == "success"
    assert response.toast.content == "已确认，正在执行。"
