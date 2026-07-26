import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import lark_oapi as lark
import pytest
from lark_oapi.api.calendar.v4 import CreateCalendarEventRequest
from lark_oapi.core.enum import AccessTokenType
from lark_oapi.core.token import verify

from song_agent.config import Settings
from song_agent.domain.commands import (
    CalendarDeleteCommand,
    CalendarUpdateCommand,
    TaskCreateCommand,
    TaskQueryCommand,
    TaskTargetCommand,
    TaskUpdateCommand,
)
from song_agent.feishu.openapi import FeishuOpenApi
from song_agent.models import DailyRecord, PlanTask, UserTokenContext


def make_settings() -> Settings:
    return Settings(
        feishu_app_id="cli-test",
        feishu_app_secret="secret",
        llm_base_url="https://llm.example/v1",
        llm_api_key="key",
        llm_model="model",
    )


def make_record() -> DailyRecord:
    return DailyRecord(
        key="_:cli-test:chat:thread:user:2026-07-24",
        date="2026-07-24",
        app_id="cli-test",
        chat_id="chat",
        thread_id="thread",
        user_id="user",
        plan_status="draft",
        tasks=[
            PlanTask(id="A1", priority="A", title="写方案", start_time="10:00"),
            PlanTask(id="B1", priority="B", title="开会", start_time="11:00"),
        ],
        created_at="2026-07-24T00:00:00+00:00",
        updated_at="2026-07-24T00:00:00+00:00",
    )


def token(subject: str, access_token: str) -> UserTokenContext:
    return UserTokenContext(
        tenant_key="tenant",
        app_id="cli-test",
        subject_id=subject,
        open_id=f"open-{subject}",
        access_token=access_token,
        expires_at=datetime.now(UTC),
        scopes=frozenset({"calendar:calendar"}),
    )


def test_sdk_client_is_configured_to_use_explicit_user_tokens() -> None:
    api = FeishuOpenApi(make_settings())
    request = (
        CreateCalendarEventRequest.builder()
        .calendar_id("primary")
        .build()
    )
    option = (
        lark.RequestOption.builder()
        .user_access_token("access-user-a")
        .build()
    )

    assert api.client._config.enable_set_token is True
    verify(api.client._config, request, option)
    assert request.token_types == {AccessTokenType.USER}
    assert option.tenant_access_token is None


@pytest.mark.asyncio
async def test_calendar_calls_use_explicit_per_user_token_and_stable_idempotency() -> None:
    captured_tokens: list[str] = []
    idempotency_keys: list[str] = []
    event_counter = 0

    def primary(request: httpx.Request) -> httpx.Response:
        captured_tokens.append(request.headers["authorization"].removeprefix("Bearer "))
        return httpx.Response(
            200,
            json={"code": 0, "data": {"calendar_id": "primary"}},
        )

    def create(request, option):
        nonlocal event_counter
        captured_tokens.append(option.user_access_token)
        idempotency_keys.append(request.idempotency_key)
        event_counter += 1
        return SimpleNamespace(
            success=lambda: True,
            data=SimpleNamespace(
                event=SimpleNamespace(
                    event_id=f"event-{event_counter}",
                    app_link=f"https://example/event-{event_counter}",
                )
            ),
            code=0,
            msg="",
        )

    api = FeishuOpenApi(make_settings())
    api.http_transport = httpx.MockTransport(primary)
    api.client = SimpleNamespace(
        calendar=SimpleNamespace(
            v4=SimpleNamespace(
                calendar_event=SimpleNamespace(create=create),
            )
        )
    )
    record = make_record()
    first = await api.create_events(record, token("a", "access-a"))
    second = await api.create_events(record, token("b", "access-b"))

    assert len(first.created) == len(second.created) == 2
    assert captured_tokens == [
        "access-a",
        "access-a",
        "access-a",
        "access-b",
        "access-b",
        "access-b",
    ]
    assert idempotency_keys[:2] == idempotency_keys[2:]
    assert len(set(idempotency_keys[:2])) == 2


@pytest.mark.asyncio
async def test_document_openapi_uses_explicit_user_token_and_direct_endpoints() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/open-apis/search/v2/doc_wiki/search":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "res_units": [
                            {
                                "title_highlighted": "<h>项目</h>方案",
                                "entity_type": "docx",
                                "result_meta": {
                                    "url": "https://feishu.cn/docx/doc-search"
                                },
                            }
                        ]
                    },
                },
            )
        if request.url.path == "/open-apis/docx/v1/documents":
            return httpx.Response(
                200,
                json={"code": 0, "data": {"document": {"document_id": "doc-new"}}},
            )
        if request.method == "GET" and request.url.path.endswith("/children"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"items": [{}, {}], "has_more": False}},
            )
        if request.method == "POST" and request.url.path.endswith("/children"):
            return httpx.Response(200, json={"code": 0, "data": {}})
        if request.url.path.endswith("/raw_content"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"content": "document body"}},
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    api = FeishuOpenApi(make_settings())
    api.http_transport = httpx.MockTransport(handler)
    user = token("user-a", "access-user-a")

    found = await api.search_documents("项目", user)
    created = await api.create_document("标题", "# 标题\n正文", user)
    appended = await api.append_document("doc-existing", "项目方案", "追加内容", user)
    content = await api.read_document("doc-existing", user)

    assert found[0].token == "doc-search"
    assert found[0].title == "项目方案"
    assert created.token == "doc-new"
    assert appended.token == "doc-existing"
    assert content == "document body"
    assert all(
        request.headers["authorization"] == "Bearer access-user-a"
        for request in requests
    )


@pytest.mark.asyncio
async def test_task_crud_uses_verified_v2_request_shapes() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"code": 0, "data": {"task": {"guid": "task-1"}}},
            )
        if request.method in {"PATCH", "GET"}:
            return httpx.Response(
                200,
                json={"code": 0, "data": {"items": [], "task": {"guid": "task-1"}}},
            )
        return httpx.Response(200, json={"code": 0, "data": {}})

    api = FeishuOpenApi(make_settings())
    api.http_transport = httpx.MockTransport(handler)
    user = token("user-a", "access-user-a")
    due = datetime(2026, 7, 27, 9, tzinfo=UTC)

    created = await api.create_task(
        TaskCreateCommand(
            summary="任务",
            due_time=due,
            assignee_open_ids=["open-user-a"],
            reminder_minutes=[10],
        ),
        user,
        idempotency_key="idempotency",
    )
    await api.query_tasks(TaskQueryCommand(completed=False), user)
    await api.update_task(
        TaskUpdateCommand(
            task_guid="task-1",
            fields={"summary": "新任务"},
        ),
        user,
    )
    await api.complete_task(TaskTargetCommand(task_guid="task-1"), user)
    deleted = await api.delete_task(TaskTargetCommand(task_guid="task-1"), user)

    create_body = json.loads(requests[0].content)
    patch_bodies = [
        json.loads(item.content) for item in requests if item.method == "PATCH"
    ]
    assert created["task_guid"] == "task-1"
    assert create_body["due"]["timestamp"] == str(int(due.timestamp() * 1000))
    assert create_body["members"][0] == {
        "id": "open-user-a",
        "type": "user",
        "role": "assignee",
    }
    assert patch_bodies[0] == {
        "task": {"summary": "新任务"},
        "update_fields": ["summary"],
    }
    assert patch_bodies[1]["update_fields"] == ["completed_at"]
    assert deleted == {"task_guid": "task-1", "deleted": True}
    assert all(
        item.headers["authorization"] == "Bearer access-user-a"
        for item in requests
    )


@pytest.mark.asyncio
async def test_calendar_update_and_delete_use_primary_user_calendar() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/calendars/primary"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"calendar_id": "primary"}},
            )
        return httpx.Response(200, json={"code": 0, "data": {}})

    api = FeishuOpenApi(make_settings())
    api.http_transport = httpx.MockTransport(handler)
    user = token("user-a", "access-user-a")
    updated = await api.update_calendar(
        CalendarUpdateCommand(event_id="event_123", summary="新标题"),
        user,
    )
    deleted = await api.delete_calendar(
        CalendarDeleteCommand(event_id="event_123"),
        user,
    )

    assert updated == {}
    assert deleted["deleted"] is True
    assert requests[1].method == "PATCH"
    assert requests[1].url.path.endswith("/events/event_123")
    assert json.loads(requests[1].content)["summary"] == "新标题"
    assert requests[2].method == "DELETE"
