from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from song_agent.application.dispatcher import ApplicationDispatcher
from song_agent.domain.intents import ExtractedIntent, UserRequest
from song_agent.intelligence.intent_extractor import (
    IntentExtractor,
    _extract_numbered_reminders,
)
from song_agent.llm import LLMInvalidResponseError
from song_agent.models import FeishuIdentity


class Llm:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = 0
        self.users = []

    async def generate(self, schema, system, user, **kwargs):
        self.users.append(user)
        output = self.outputs[self.calls]
        self.calls += 1
        if isinstance(output, Exception):
            raise output
        return schema.model_validate(output)


def request() -> UserRequest:
    return UserRequest(
        identity=FeishuIdentity(app_id="app", open_id="open"),
        text="十分钟后提醒我喝水",
        message_id="message",
    )


@pytest.mark.asyncio
async def test_intent_extractor_identifies_calendar_and_missing_fields() -> None:
    llm = Llm(
        [
            {
                "intent": "calendar.create",
                "arguments": {"summary": "喝水"},
                "missing_fields": ["start_time"],
                "confidence": 0.99,
            }
        ]
    )
    result = await IntentExtractor(llm, "Asia/Shanghai").extract(request())
    assert result.intent == "calendar.create"
    assert result.missing_fields == ["start_time"]


@pytest.mark.asyncio
async def test_intent_extractor_receives_attachment_result() -> None:
    llm = Llm(
        [
            {
                "intent": "conversation.general",
                "confidence": 1,
            }
        ]
    )
    image_request = request().model_copy(
        update={
            "text": "这个报错怎么解决？",
            "context": {
                "retrieved_context": [
                    {
                        "source_type": "image",
                        "analysis": "图片显示数据库连接超时",
                    }
                ]
            },
        }
    )

    await IntentExtractor(llm, "Asia/Shanghai").extract(image_request)

    assert "本次附件解析结果" in llm.users[0]
    assert "数据库连接超时" in llm.users[0]


@pytest.mark.asyncio
async def test_planning_request_routes_to_general_without_llm() -> None:
    llm = Llm([])
    planning_request = request().model_copy(
        update={
            "text": "我今天想要在1点开会、2点吃饭、3点的时候去购物，你给我规划一下"
        }
    )

    result = await IntentExtractor(llm, "Asia/Shanghai").extract(planning_request)

    assert result.intent == "conversation.general"
    assert result.missing_fields == []
    assert llm.calls == 0


@pytest.mark.asyncio
async def test_document_request_routes_to_general_without_intent_llm() -> None:
    llm = Llm([])
    document_request = request().model_copy(
        update={"text": "把这句话写入到 hermes 群中的每日记录云文档中"}
    )

    result = await IntentExtractor(llm, "Asia/Shanghai").extract(document_request)

    assert result.intent == "conversation.general"
    assert result.missing_fields == []
    assert llm.calls == 0


@pytest.mark.asyncio
async def test_general_intent_discards_model_invented_missing_fields() -> None:
    llm = Llm(
        [
            {
                "intent": "conversation.general",
                "missing_fields": [
                    "text_to_append",
                    "document_name",
                    "append_position",
                ],
                "confidence": 0.99,
            }
        ]
    )

    result = await IntentExtractor(llm, "Asia/Shanghai").extract(
        request().model_copy(update={"text": "我现在说话你能听见吗？"})
    )

    assert result.intent == "conversation.general"
    assert result.missing_fields == []


@pytest.mark.asyncio
async def test_audio_intent_excludes_old_conversation_from_classifier() -> None:
    llm = Llm(
        [
            {
                "intent": "conversation.general",
                "confidence": 0.99,
            }
        ]
    )
    audio_request = request().model_copy(
        update={
            "text": "今天天气怎么样？",
            "context": {"attachment_kinds": ["audio"]},
        }
    )
    business = SimpleNamespace(
        recent_messages=[
            SimpleNamespace(role="user", content="把这句话写入每日记录云文档")
        ],
        memories=[],
        conversation_summary=None,
        active_pending_action=None,
    )

    await IntentExtractor(llm, "Asia/Shanghai").extract(audio_request, business)

    assert "最近对话" not in llm.users[0]
    assert "每日记录云文档" not in llm.users[0]


@pytest.mark.asyncio
async def test_explicit_calendar_write_is_not_overridden_by_planning_phrase() -> None:
    llm = Llm(
        [
            {
                "intent": "calendar.create",
                "arguments": {
                    "summary": "今日安排",
                    "start_time": "2026-07-27T13:00:00+08:00",
                },
                "missing_fields": [],
                "confidence": 0.99,
            }
        ]
    )
    planning_request = request().model_copy(
        update={"text": "帮我规划一下，并创建日程"}
    )

    result = await IntentExtractor(llm, "Asia/Shanghai").extract(planning_request)

    assert result.intent == "calendar.create"
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_optional_batch_reminder_end_times_are_not_missing() -> None:
    llm = Llm(
        [
            {
                "intent": "reminder.batch_create",
                "arguments": {
                    "items": [
                        {
                            "summary": "开会",
                            "start_time": "2026-07-27T13:00:00+08:00",
                        },
                        {
                            "summary": "吃饭",
                            "start_time": "2026-07-27T14:00:00+08:00",
                        },
                    ]
                },
                "missing_fields": [
                    "items[0].end_time",
                    "items[1].end_time",
                ],
                "confidence": 0.99,
            }
        ]
    )
    reminder_request = request().model_copy(
        update={"text": "下午1点提醒我开会，下午2点提醒我吃饭"}
    )

    result = await IntentExtractor(llm, "Asia/Shanghai").extract(reminder_request)

    assert result.intent == "reminder.batch_create"
    assert result.missing_fields == []


@pytest.mark.asyncio
async def test_intent_extractor_repairs_once() -> None:
    llm = Llm(
        [
            LLMInvalidResponseError("invalid"),
            ExtractedIntent(
                intent="calendar.query",
                confidence=0.9,
            ),
        ]
    )
    result = await IntentExtractor(llm, "Asia/Shanghai").extract(request())
    assert result.intent == "calendar.query"
    assert llm.calls == 2


@pytest.mark.asyncio
async def test_intent_extractor_accepts_intent_type_and_routes_document_to_agent() -> None:
    llm = Llm(
        [
            {
                "intent_type": "document.append",
                "arguments": {"target_title": "每日记录"},
                "missing_fields": [],
                "confidence": 0.99,
            }
        ]
    )

    result = await IntentExtractor(llm, "Asia/Shanghai").extract(request())

    assert result.intent == "conversation.general"
    assert result.arguments == {"target_title": "每日记录"}
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_intent_extractor_falls_back_to_agent_after_two_invalid_outputs() -> None:
    llm = Llm(
        [
            LLMInvalidResponseError("invalid first"),
            LLMInvalidResponseError("invalid repair"),
        ]
    )

    result = await IntentExtractor(llm, "Asia/Shanghai").extract(request())

    assert result.intent == "conversation.general"
    assert result.confidence == 1.0
    assert llm.calls == 2


def test_numbered_reminders_are_parsed_without_llm() -> None:
    result = _extract_numbered_reminders(
        "给我定两个闹钟：\n"
        "1. 明天早上6点钟起床的闹钟\n"
        "2. 每天晚上9点复盘的闹钟",
        "Asia/Shanghai",
        now=datetime(2026, 7, 26, 23, 13, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert result
    assert result.intent == "reminder.batch_create"
    assert result.arguments["items"] == [
        {
            "summary": "起床",
            "start_time": "2026-07-27T06:00:00+08:00",
            "timezone": "Asia/Shanghai",
            "reminder_minutes": [0],
            "recurrence": None,
        },
        {
            "summary": "复盘",
            "start_time": "2026-07-27T21:00:00+08:00",
            "timezone": "Asia/Shanghai",
            "reminder_minutes": [0],
            "recurrence": "FREQ=DAILY",
        },
    ]


@pytest.mark.asyncio
async def test_low_confidence_does_not_dispatch_external_operation() -> None:
    class Extractor:
        async def extract(self, user_request, business_context):
            return ExtractedIntent(
                intent="calendar.create",
                arguments={"summary": "喝水"},
                confidence=0.4,
            )

    class Calendar:
        called = False

        async def prepare_create(self, user_request, arguments):
            self.called = True
            raise AssertionError("low confidence must not prepare an action")

    class General:
        async def run(self, user_request):
            raise AssertionError("deterministic intent must not reach ReAct")

    class Contexts:
        async def record_user(self, user_request):
            pass

        async def record_assistant(self, user_request, content):
            pass

    class BusinessContexts:
        async def build_for_intent_extraction(self, user_request):
            return object()

    calendar = Calendar()
    dispatcher = ApplicationDispatcher(
        Extractor(),
        General(),
        BusinessContexts(),
        Contexts(),
        object(),
    )
    dispatcher.register("calendar.create", calendar.prepare_create)
    result = await dispatcher.dispatch(request())
    assert result.status == "clarification_required"
    assert calendar.called is False
