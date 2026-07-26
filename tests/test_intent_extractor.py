from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from song_agent.application.request_router import RequestRouter
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

    async def generate(self, schema, system, user, **kwargs):
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
    router = RequestRouter(
        Extractor(),
        calendar,
        object(),
        object(),
        object(),
        General(),
        BusinessContexts(),
        Contexts(),
        object(),
    )
    result = await router.handle(request())
    assert result.status == "clarification_required"
    assert calendar.called is False
