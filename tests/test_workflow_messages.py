import logging
from types import SimpleNamespace

import pytest

from song_agent.domain.results import ApplicationResult
from song_agent.models import IncomingMessage
from song_agent.workflow import AgentWorkflow


class Store:
    async def claim_event(self, *args, **kwargs):
        return True

    async def get_record(self, *args, **kwargs):
        return None


class Transport:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    async def send_markdown(self, chat_id: str, markdown: str):
        self.messages.append((chat_id, markdown))
        return f"message-{len(self.messages)}"


class Router:
    async def handle(self, request):
        return ApplicationResult(status="ok", message="处理完成")


def incoming(text: str = "杭州萧山天气怎么样") -> IncomingMessage:
    return IncomingMessage(
        message_id="message-1",
        event_id="event-1",
        tenant_key="tenant",
        app_id="app",
        user_id="user-1",
        open_id="open-1",
        chat_id="chat-1",
        chat_type="p2p",
        message_type="text",
        text=text,
    )


def workflow() -> AgentWorkflow:
    instance = object.__new__(AgentWorkflow)
    instance.store = Store()
    instance.transport = Transport()
    instance.oauth = SimpleNamespace(settings=SimpleNamespace(feishu_app_id="app"))
    instance.planner = SimpleNamespace(today=lambda: "2026-07-26")
    instance.request_router = Router()
    instance.logger = logging.getLogger("test.workflow.messages")
    return instance


@pytest.mark.asyncio
async def test_general_request_logs_content_and_sends_processing_status(caplog) -> None:
    instance = workflow()

    with caplog.at_level(logging.INFO, logger="test.workflow.messages"):
        await instance._handle(incoming())

    assert "content='杭州萧山天气怎么样'" in caplog.text
    assert instance.transport.messages == [
        ("chat-1", "⏳ 正在处理你的请求，请稍候…"),
        ("chat-1", "处理完成"),
    ]
