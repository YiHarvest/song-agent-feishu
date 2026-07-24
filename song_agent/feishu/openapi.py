"""无状态的飞书 OpenAPI 适配器，用于生产环境的用户级写入操作。"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
import lark_oapi as lark
from lark_oapi.api.calendar.v4 import (
    CalendarEvent,
    CreateCalendarEventRequest,
    PrimaryCalendarRequest,
    Reminder,
    TimeInfo,
    Vchat,
)

from ..config import Settings
from ..models import DailyRecord, PlanTask, UserTokenContext
from .mcp import CreatedDocument, FoundDocument, markdown_to_text_blocks, sanitize_title


@dataclass
class CalendarCreationResult:
    created: list[tuple[str, str]] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


class FeishuOpenApi:
    """直接飞书 SDK 适配器，不共享可变用户授权状态。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.logger = logging.getLogger(__name__)
        self.http_transport: httpx.AsyncBaseTransport | None = None
        self.client = (
            lark.Client.builder()
            .app_id(settings.feishu_app_id)
            .app_secret(settings.feishu_app_secret)
            .domain(settings.domain)
            .build()
        )

    async def search_documents(
        self,
        search_key: str,
        token_context: UserTokenContext,
        *,
        page_size: int = 20,
    ) -> list[FoundDocument]:
        data = await self._request(
            "POST",
            "/open-apis/search/v2/doc_wiki/search",
            token_context,
            json={
                "query": search_key,
                "page_size": min(max(page_size, 1), 20),
                "doc_filter": {},
                "wiki_filter": {},
            },
        )
        found: dict[str, FoundDocument] = {}
        for item in data.get("res_units", []):
            if not isinstance(item, dict):
                continue
            meta = item.get("result_meta")
            meta = meta if isinstance(meta, dict) else {}
            url = str(meta.get("url") or item.get("url") or "")
            token = _document_token(item, meta, url)
            title = re.sub(
                r"</?h>",
                "",
                str(item.get("title_highlighted") or item.get("title") or ""),
            ).strip()
            if token and title and ("/docx/" in url or _is_docx(item, meta)):
                found[token] = FoundDocument(
                    title=title,
                    token=token,
                    url=url or f"https://feishu.cn/docx/{token}",
                )
        return list(found.values())

    async def read_document(
        self,
        document_id: str,
        token_context: UserTokenContext,
    ) -> str:
        data = await self._request(
            "GET",
            f"/open-apis/docx/v1/documents/{document_id}/raw_content",
            token_context,
        )
        return str(data.get("content") or "")

    async def create_document(
        self,
        title: str,
        markdown: str,
        token_context: UserTokenContext,
    ) -> CreatedDocument:
        data = await self._request(
            "POST",
            "/open-apis/docx/v1/documents",
            token_context,
            json={"title": sanitize_title(title)},
        )
        document = data.get("document") if isinstance(data.get("document"), dict) else data
        document_id = str(document.get("document_id") or "")
        if not document_id:
            raise RuntimeError("飞书 OpenAPI 返回成功，但缺少 document_id")
        await self._append_document_blocks(document_id, markdown, token_context, start_index=0)
        return CreatedDocument(
            title=title,
            token=document_id,
            url=f"https://feishu.cn/docx/{document_id}",
        )

    async def append_document(
        self,
        document_id: str,
        title: str,
        markdown: str,
        token_context: UserTokenContext,
    ) -> CreatedDocument:
        index = 0
        page_token = ""
        while True:
            params: dict[str, str | int] = {"page_size": 500}
            if page_token:
                params["page_token"] = page_token
            data = await self._request(
                "GET",
                f"/open-apis/docx/v1/documents/{document_id}/blocks/{document_id}/children",
                token_context,
                params=params,
            )
            items = data.get("items", [])
            index += len(items) if isinstance(items, list) else 0
            if not data.get("has_more"):
                break
            page_token = str(data.get("page_token") or "")
            if not page_token:
                break
        await self._append_document_blocks(
            document_id,
            markdown,
            token_context,
            start_index=index,
        )
        return CreatedDocument(
            title=title,
            token=document_id,
            url=f"https://feishu.cn/docx/{document_id}",
        )

    async def _append_document_blocks(
        self,
        document_id: str,
        markdown: str,
        token_context: UserTokenContext,
        *,
        start_index: int,
    ) -> None:
        blocks = markdown_to_text_blocks(markdown, "")
        for offset in range(0, len(blocks), 50):
            await self._request(
                "POST",
                f"/open-apis/docx/v1/documents/{document_id}/blocks/{document_id}/children",
                token_context,
                json={
                    "children": blocks[offset : offset + 50],
                    "index": start_index + offset,
                },
            )

    async def _request(
        self,
        method: str,
        path: str,
        token_context: UserTokenContext,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, str | int] | None = None,
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(
            base_url=self.settings.domain,
            timeout=20,
            transport=self.http_transport,
            trust_env=False,
        ) as client:
            response = await client.request(
                method,
                path,
                headers={"Authorization": f"Bearer {token_context.access_token}"},
                json=json,
                params=params,
            )
        try:
            payload = response.json()
        except ValueError as error:
            raise RuntimeError(
                f"飞书 OpenAPI 返回非 JSON 响应: HTTP {response.status_code}"
            ) from error
        if response.is_error or payload.get("code"):
            raise RuntimeError(
                payload.get("msg") or f"飞书 OpenAPI 请求失败: HTTP {response.status_code}"
            )
        data = payload.get("data", {})
        return data if isinstance(data, dict) else {}

    async def create_events(
        self,
        record: DailyRecord,
        token_context: UserTokenContext,
        task_ids: set[str] | None = None,
    ) -> CalendarCreationResult:
        output = CalendarCreationResult()
        selected = [task for task in record.tasks if task_ids is None or task.id in task_ids]
        candidates = [task for task in selected if task.start_time and not task.calendar_event_id]
        output.skipped = [task.id for task in selected if not task.start_time]
        if not candidates:
            return output
        calendar_id = self.settings.feishu_calendar_id or await self._primary_calendar_id(token_context)
        for task in candidates:
            try:
                event_id = await self._create_event(
                    calendar_id,
                    record,
                    task,
                    token_context,
                    idempotency_key=_event_idempotency_key(record, task),
                )
                output.created.append((task.id, event_id))
            except Exception as error:
                output.failed.append((task.id, str(error)))
                self.logger.exception(
                    "通过飞书 OpenAPI 创建日程失败 task=%s subject=%s",
                    task.id,
                    token_context.subject_id,
                )
        return output

    async def _primary_calendar_id(self, token_context: UserTokenContext) -> str:
        request = PrimaryCalendarRequest.builder().user_id_type("open_id").build()
        response = await asyncio.to_thread(
            self.client.calendar.v4.calendar.primary,
            request,
            _request_option(token_context),
        )
        if not response.success():
            raise RuntimeError(f"获取主日历失败: {response.code} {response.msg}")
        calendars = response.data.calendars if response.data else None
        calendar_id = calendars[0].calendar.calendar_id if calendars and calendars[0].calendar else None
        if not calendar_id:
            raise RuntimeError("飞书 OpenAPI 未返回用户主日历 ID")
        return calendar_id

    async def _create_event(
        self,
        calendar_id: str,
        record: DailyRecord,
        task: PlanTask,
        token_context: UserTokenContext,
        *,
        idempotency_key: str,
    ) -> str:
        request = (
            CreateCalendarEventRequest.builder()
            .calendar_id(calendar_id)
            .idempotency_key(idempotency_key)
            .user_id_type("open_id")
            .request_body(_calendar_event(self.settings, record, task))
            .build()
        )
        response = await asyncio.to_thread(
            self.client.calendar.v4.calendar_event.create,
            request,
            _request_option(token_context),
        )
        if not response.success():
            raise RuntimeError(f"创建日程失败: {response.code} {response.msg}")
        event = response.data.event if response.data else None
        if not event or not event.event_id:
            raise RuntimeError("飞书 OpenAPI 返回成功，但缺少 event_id")
        return event.event_id


def _request_option(token_context: UserTokenContext) -> lark.RequestOption:
    return (
        lark.RequestOption.builder()
        .tenant_key(token_context.tenant_key)
        .user_access_token(token_context.access_token)
        .build()
    )


def _calendar_event(settings: Settings, record: DailyRecord, task: PlanTask) -> CalendarEvent:
    zone = ZoneInfo(settings.timezone)
    start = datetime.fromisoformat(f"{record.date}T{task.start_time}").replace(tzinfo=zone)
    end = (
        datetime.fromisoformat(f"{record.date}T{task.end_time}").replace(tzinfo=zone)
        if task.end_time
        else start + timedelta(minutes=30)
    )
    if end <= start:
        end += timedelta(days=1)
    builder = (
        CalendarEvent.builder()
        .summary(f"[{task.id}] {task.title}")
        .description(f"由 Song Agent 根据 {record.date} 日计划创建。")
        .start_time(
            TimeInfo.builder()
            .timestamp(str(int(start.timestamp())))
            .timezone(settings.timezone)
            .build()
        )
        .end_time(
            TimeInfo.builder()
            .timestamp(str(int(end.timestamp())))
            .timezone(settings.timezone)
            .build()
        )
        .reminders([Reminder.builder().minutes(10).build()])
        .vchat(Vchat.builder().vc_type("no_meeting").build())
    )
    recurrence = {
        "daily": "FREQ=DAILY",
        "weekdays": "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
        "weekly": "FREQ=WEEKLY",
    }.get(task.repeat)
    if recurrence:
        builder.recurrence(recurrence)
    return builder.build()


def _event_idempotency_key(record: DailyRecord, task: PlanTask) -> str:
    source = f"{record.key}:{record.created_at}:{task.id}".encode()
    return hashlib.sha256(source).hexdigest()


def _document_token(item: dict[str, Any], meta: dict[str, Any], url: str) -> str:
    for source in (meta, item):
        for key in ("document_id", "doc_token", "docs_token", "obj_token", "token"):
            value = source.get(key)
            if isinstance(value, str) and value:
                return value
    match = re.search(r"/docx/([A-Za-z0-9_-]+)", urlparse(url).path)
    return match.group(1) if match else ""


def _is_docx(item: dict[str, Any], meta: dict[str, Any]) -> bool:
    values = (item.get("entity_type"), meta.get("doc_types"), meta.get("doc_type"))
    return any("docx" in str(value).lower() for value in values)
