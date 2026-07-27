import logging
from types import SimpleNamespace

import pytest

from song_agent.attachments.service import PreparedAttachmentMessage
from song_agent.domain.results import ApplicationResult
from song_agent.media.vision_client import VisionBusyError, VisionTimeoutError
from song_agent.models import IncomingAttachmentRef, IncomingMessage
from song_agent.workflow import AgentWorkflow


class Store:
    async def claim_event(self, *args, **kwargs):
        return True

    async def get_record(self, *args, **kwargs):
        return None


class BatchStore(Store):
    async def get_pending_action(self, action_id):
        return SimpleNamespace(
            action_id=action_id,
            action_type="reminder.create",
            payload={"summary": action_id},
        )


class Transport:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    async def send_markdown(self, chat_id: str, markdown: str):
        self.messages.append((chat_id, markdown))
        return f"message-{len(self.messages)}"


class BatchTransport(Transport):
    def __init__(self) -> None:
        super().__init__()
        self.confirmations = []

    async def send_confirmation_card(self, chat_id, markdown, action):
        self.confirmations.append((chat_id, action.action_id))
        return f"confirmation-{len(self.confirmations)}"


class Router:
    async def handle(self, request):
        return ApplicationResult(status="ok", message="处理完成")


class ConversationContexts:
    def __init__(self) -> None:
        self.recorded: list[tuple[str, str]] = []

    async def record_user(self, request) -> None:
        self.recorded.append(("user", request.text))

    async def record_assistant(self, request, content) -> None:
        del request
        self.recorded.append(("assistant", content))


class ContextRecordingRouter(Router):
    def __init__(self) -> None:
        self.conversation_contexts = ConversationContexts()


class BatchRouter:
    def __init__(self, action_ids):
        self.action_ids = action_ids

    async def handle(self, request):
        return ApplicationResult(
            status="awaiting_confirmation",
            intent="reminder.batch_create",
            message=f"已准备 {len(self.action_ids)} 个提醒。",
            action_id=self.action_ids[0],
            data={"action_ids": self.action_ids},
        )


class DirectAttachmentService:
    async def prepare(self, message, text):
        del message, text
        return PreparedAttachmentMessage(
            text="这是什么",
            context={"source_type": "attachment"},
            direct_response="图片显示数据库连接超时。",
        )


class TimeoutAttachmentService:
    async def prepare(self, message, text):
        del message, text
        raise VisionTimeoutError("timeout")


class BusyAttachmentService:
    async def prepare(self, message, text):
        del message, text
        raise VisionBusyError("busy")


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


@pytest.mark.parametrize("action_count", [3, 5])
@pytest.mark.asyncio
async def test_batch_result_sends_each_confirmation_card(action_count: int) -> None:
    action_ids = [f"action-{index}" for index in range(1, action_count + 1)]
    instance = workflow()
    instance.store = BatchStore()
    instance.transport = BatchTransport()
    instance.request_router = BatchRouter(action_ids)

    await instance._handle(incoming(f"创建{action_count}个闹钟"))

    assert instance.transport.confirmations == [
        ("chat-1", action_id) for action_id in action_ids
    ]


@pytest.mark.asyncio
async def test_direct_attachment_answer_skips_router() -> None:
    instance = workflow()
    instance.attachment_service = DirectAttachmentService()
    instance.request_router = ContextRecordingRouter()
    media = incoming("").model_copy(
        update={
            "message_type": "image",
            "attachments": [
                IncomingAttachmentRef(
                    kind="image",
                    resource_key="image-key",
                    resource_type="image",
                    filename="image.png",
                )
            ],
        }
    )

    await instance._handle(media)

    assert instance.transport.messages == [
        ("chat-1", "⏳ 已收到图片，正在读取和分析…"),
        ("chat-1", "图片显示数据库连接超时。"),
    ]
    assert instance.request_router.conversation_contexts.recorded == [
        ("user", "这是什么"),
        ("assistant", "图片显示数据库连接超时。"),
    ]


@pytest.mark.asyncio
async def test_image_timeout_is_reported_without_generic_failure() -> None:
    instance = workflow()
    instance.attachment_service = TimeoutAttachmentService()
    instance.oauth.settings.song_agent_vision_read_timeout_seconds = 45
    instance.oauth.settings.song_agent_vision_max_retries = 0
    media = incoming("").model_copy(
        update={
            "message_type": "image",
            "attachments": [
                IncomingAttachmentRef(
                    kind="image",
                    resource_key="image-key",
                    resource_type="image",
                    filename="image.png",
                )
            ],
        }
    )

    await instance._handle(media)

    assert instance.transport.messages == [
        ("chat-1", "⏳ 已收到图片，正在读取和分析…"),
        (
            "chat-1",
            "图片已经收到，但图片理解服务响应超时；本次没有自动重试。请稍后重新发送。",
        ),
    ]


@pytest.mark.asyncio
async def test_image_provider_overload_is_reported_without_traceback() -> None:
    instance = workflow()
    instance.attachment_service = BusyAttachmentService()
    instance.oauth.settings.song_agent_vision_max_retries = 0
    media = incoming("").model_copy(
        update={
            "message_type": "image",
            "attachments": [
                IncomingAttachmentRef(
                    kind="image",
                    resource_key="image-key",
                    resource_type="image",
                    filename="image.png",
                )
            ],
        }
    )

    await instance._handle(media)

    assert instance.transport.messages == [
        ("chat-1", "⏳ 已收到图片，正在读取和分析…"),
        (
            "chat-1",
            "图片已经收到，但图片理解服务当前繁忙；本次没有自动重试。请稍后重新发送。",
        ),
    ]
