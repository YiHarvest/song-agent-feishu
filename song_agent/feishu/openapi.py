"""无状态的飞书 OpenAPI 适配器，用于生产环境的用户级写入操作。"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
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
    Reminder,
    TimeInfo,
    Vchat,
)
from lark_oapi.api.calendar.v4.model.event_location import EventLocation

from ..config import Settings
from ..domain.commands import (
    CalendarCreateCommand,
    CalendarDeleteCommand,
    CalendarQueryCommand,
    CalendarUpdateCommand,
    TaskCreateCommand,
    TaskQueryCommand,
    TaskTargetCommand,
    TaskUpdateCommand,
)
from ..models import DailyRecord, PlanTask, UserTokenContext
from .mcp import CreatedDocument, FoundDocument, markdown_to_text_blocks, sanitize_title


@dataclass
class CalendarCreationResult:
    created: list[tuple[str, str]] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


class FeishuApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: int = 0,
        retryable: bool = False,
        request_id: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.request_id = request_id


class FeishuOpenApi:
    """直接飞书 SDK 适配器，不共享可变用户授权状态。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.logger = logging.getLogger(__name__)
        self.http_transport: httpx.AsyncBaseTransport | None = None
        self._primary_calendar_cache: dict[str, str] = {}
        self.client = (
            lark.Client.builder()
            .app_id(settings.feishu_app_id)
            .app_secret(settings.feishu_app_secret)
            .domain(settings.domain)
            .enable_set_token(True)
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
            raise FeishuApiError(
                f"飞书主日历接口返回非 JSON: HTTP {response.status_code}",
                code=response.status_code,
                retryable=response.status_code >= 500,
                request_id=_response_request_id(response),
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
        self.logger.info(
            "创建日程批次开始 subject=%s selected=%s candidates=%s skipped=%s "
            "configured_calendar=%s",
            token_context.subject_id,
            [task.id for task in selected],
            [task.id for task in candidates],
            output.skipped,
            bool(self.settings.feishu_calendar_id),
        )
        if not candidates:
            return output
        calendar_id = self.settings.feishu_calendar_id or await self._primary_calendar_id(token_context)
        self.logger.info(
            "日程目标日历已确定 subject=%s calendar_id=%s source=%s",
            token_context.subject_id,
            calendar_id,
            "configured" if self.settings.feishu_calendar_id else "primary",
        )
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
                    "通过飞书 OpenAPI 创建日程失败 task=%s subject=%s "
                    "calendar_id=%s error=%s",
                    task.id,
                    token_context.subject_id,
                    calendar_id,
                    error,
                )
        self.logger.info(
            "创建日程批次结束 subject=%s created=%s failed=%s skipped=%s",
            token_context.subject_id,
            output.created,
            output.failed,
            output.skipped,
        )
        return output

    async def create_calendar_command(
        self,
        command: CalendarCreateCommand,
        token_context: UserTokenContext,
        *,
        idempotency_key: str,
    ) -> dict[str, str]:
        calendar_id = await self._primary_calendar_id(token_context)
        event = _calendar_event_from_command(command)
        try:
            event_id, request_id, app_link = await self._create_calendar_event_model(
                calendar_id,
                event,
                token_context,
                idempotency_key=idempotency_key,
            )
        except FeishuApiError as error:
            if error.code in {191003, 191004}:
                self._primary_calendar_cache.pop(token_context.subject_id, None)
                calendar_id = await self._primary_calendar_id(token_context, force=True)
                event_id, request_id, app_link = await self._create_calendar_event_model(
                    calendar_id,
                    event,
                    token_context,
                    idempotency_key=idempotency_key,
                )
            else:
                raise
        if command.attendee_open_ids:
            try:
                await self._business_request(
                    "POST",
                    (
                        f"/open-apis/calendar/v4/calendars/{calendar_id}"
                        f"/events/{event_id}/attendees"
                    ),
                    token_context,
                    json={
                        "attendees": [
                            {"type": "user", "user_id": open_id}
                            for open_id in command.attendee_open_ids
                        ],
                        "need_notification": True,
                    },
                    params={"user_id_type": "open_id"},
                )
            except FeishuApiError as error:
                raise FeishuApiError(
                    f"日程已创建，但添加参与人失败: {error}",
                    code=error.code,
                    retryable=error.retryable,
                    request_id=error.request_id,
                ) from error
        return {
            "event_id": event_id,
            "calendar_id": calendar_id,
            "url": app_link
            or f"https://applink.feishu.cn/client/calendar/event/detail?eventId={event_id}",
            "request_id": request_id,
        }

    async def query_calendar(
        self,
        command: CalendarQueryCommand,
        token_context: UserTokenContext,
    ) -> dict[str, Any]:
        calendar_id = await self._primary_calendar_id(token_context)
        if command.event_id:
            return await self._business_request(
                "GET",
                f"/open-apis/calendar/v4/calendars/{calendar_id}/events/{command.event_id}",
                token_context,
                params={"user_id_type": "open_id"},
            )
        if command.query:
            payload: dict[str, Any] = {"query": command.query}
            if command.start_time and command.end_time:
                payload["filter"] = {
                    "time_range": {
                        "start_time": command.start_time.isoformat(),
                        "end_time": command.end_time.isoformat(),
                    }
                }
            return await self._business_request(
                "POST",
                f"/open-apis/calendar/v4/calendars/{calendar_id}/events/search",
                token_context,
                json=payload,
                params={"page_size": command.page_size, "user_id_type": "open_id"},
            )
        now = datetime.now(ZoneInfo(self.settings.timezone))
        start = command.start_time or now
        end = command.end_time or (start + timedelta(days=7))
        return await self._business_request(
            "GET",
            f"/open-apis/calendar/v4/calendars/{calendar_id}/events/instance_view",
            token_context,
            params={
                "start_time": str(int(start.timestamp())),
                "end_time": str(int(end.timestamp())),
                "user_id_type": "open_id",
            },
        )

    async def update_calendar(
        self,
        command: CalendarUpdateCommand,
        token_context: UserTokenContext,
    ) -> dict[str, Any]:
        calendar_id = command.calendar_id or await self._primary_calendar_id(token_context)
        event_id = _event_id_for_scope(command.event_id, command.recurrence_scope)
        body: dict[str, Any] = {"need_notification": True}
        for field_name in ("summary", "description", "recurrence"):
            value = getattr(command, field_name)
            if value is not None:
                body[field_name] = value
        if command.start_time and command.end_time:
            body["start_time"] = {
                "timestamp": str(int(command.start_time.timestamp())),
                "timezone": command.timezone,
            }
            body["end_time"] = {
                "timestamp": str(int(command.end_time.timestamp())),
                "timezone": command.timezone,
            }
        if command.location is not None:
            body["location"] = {"name": command.location}
        if command.reminder_minutes is not None:
            body["reminders"] = [
                {"minutes": minutes} for minutes in command.reminder_minutes
            ]
        return await self._business_request(
            "PATCH",
            f"/open-apis/calendar/v4/calendars/{calendar_id}/events/{event_id}",
            token_context,
            json=body,
            params={"user_id_type": "open_id"},
        )

    async def delete_calendar(
        self,
        command: CalendarDeleteCommand,
        token_context: UserTokenContext,
    ) -> dict[str, Any]:
        calendar_id = command.calendar_id or await self._primary_calendar_id(token_context)
        event_id = _event_id_for_scope(command.event_id, command.recurrence_scope)
        await self._business_request(
            "DELETE",
            f"/open-apis/calendar/v4/calendars/{calendar_id}/events/{event_id}",
            token_context,
            params={"need_notification": "true"},
        )
        return {"event_id": event_id, "calendar_id": calendar_id, "deleted": True}

    async def create_task(
        self,
        command: TaskCreateCommand,
        token_context: UserTokenContext,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        task: dict[str, Any] = {
            "summary": command.summary,
            "description": command.description,
            "client_token": idempotency_key,
        }
        if command.start_time:
            task["start"] = _task_time(command.start_time, command.is_all_day)
        if command.due_time:
            task["due"] = _task_time(command.due_time, command.is_all_day)
        members = [
            {"id": open_id, "type": "user", "role": "assignee"}
            for open_id in command.assignee_open_ids
        ]
        members.extend(
            {"id": open_id, "type": "user", "role": "follower"}
            for open_id in command.follower_open_ids
        )
        if members:
            task["members"] = members
        if command.reminder_minutes:
            task["reminders"] = [
                {"relative_fire_minute": value}
                for value in command.reminder_minutes
            ]
        if command.repeat_rule:
            task["repeat_rule"] = command.repeat_rule
        if command.tasklist_guid:
            task["tasklists"] = [{"tasklist_guid": command.tasklist_guid}]
        data = await self._business_request(
            "POST",
            "/open-apis/task/v2/tasks",
            token_context,
            json=task,
            params={"user_id_type": "open_id"},
        )
        return _task_result(data)

    async def query_tasks(
        self,
        command: TaskQueryCommand,
        token_context: UserTokenContext,
    ) -> dict[str, Any]:
        if command.task_guid:
            return await self._business_request(
                "GET",
                f"/open-apis/task/v2/tasks/{command.task_guid}",
                token_context,
                params={"user_id_type": "open_id"},
            )
        params: dict[str, str | int] = {
            "type": "my_tasks",
            "page_size": command.page_size,
            "user_id_type": "open_id",
        }
        if command.completed is not None:
            params["completed"] = str(command.completed).lower()
        data = await self._business_request(
            "GET",
            "/open-apis/task/v2/tasks",
            token_context,
            params=params,
        )
        if command.query and isinstance(data.get("items"), list):
            query = command.query.casefold()
            data["items"] = [
                item
                for item in data["items"]
                if query in str(item.get("summary") or "").casefold()
            ]
        return data

    async def update_task(
        self,
        command: TaskUpdateCommand,
        token_context: UserTokenContext,
    ) -> dict[str, Any]:
        update_fields = sorted(command.fields.model_fields_set)
        fields = command.fields.model_dump(
            mode="json",
            include=command.fields.model_fields_set,
        )
        return await self._business_request(
            "PATCH",
            f"/open-apis/task/v2/tasks/{command.task_guid}",
            token_context,
            json={"task": fields, "update_fields": update_fields},
            params={"user_id_type": "open_id"},
        )

    async def complete_task(
        self,
        command: TaskTargetCommand,
        token_context: UserTokenContext,
    ) -> dict[str, Any]:
        completed_at = str(int(datetime.now().timestamp() * 1000))
        return await self.update_task(
            TaskUpdateCommand(
                task_guid=command.task_guid,
                fields={"completed_at": completed_at},
            ),
            token_context,
        )

    async def delete_task(
        self,
        command: TaskTargetCommand,
        token_context: UserTokenContext,
    ) -> dict[str, Any]:
        await self._business_request(
            "DELETE",
            f"/open-apis/task/v2/tasks/{command.task_guid}",
            token_context,
        )
        return {"task_guid": command.task_guid, "deleted": True}

    async def _business_request(
        self,
        method: str,
        path: str,
        token_context: UserTokenContext,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, str | int] | None = None,
    ) -> dict[str, Any]:
        started_at = time.monotonic()
        self.logger.info(
            "飞书业务请求开始 method=%s path=%s subject=%s body_fields=%s params=%s",
            method,
            path,
            token_context.subject_id,
            sorted(json) if json else [],
            sorted(params) if params else [],
        )
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
        request_id = _response_request_id(response)
        try:
            payload = response.json() if response.content else {}
        except ValueError as error:
            raise FeishuApiError(
                f"飞书 OpenAPI 返回非 JSON: HTTP {response.status_code}",
                code=response.status_code,
                retryable=response.status_code >= 500,
                request_id=request_id,
            ) from error
        raw_code = payload.get("code") or (
            response.status_code if response.is_error else 0
        )
        code = int(raw_code)
        if response.is_error or code:
            self.logger.error(
                "飞书业务请求失败 method=%s path=%s subject=%s http_status=%d "
                "code=%s request_id=%s duration_ms=%d msg=%s",
                method,
                path,
                token_context.subject_id,
                response.status_code,
                code,
                request_id,
                int((time.monotonic() - started_at) * 1000),
                payload.get("msg"),
            )
            raise FeishuApiError(
                str(payload.get("msg") or f"HTTP {response.status_code}"),
                code=code,
                retryable=(
                    response.status_code == 429
                    or response.status_code >= 500
                    or code in {99991400, 99991401}
                ),
                request_id=request_id,
            )
        data = payload.get("data", {})
        self.logger.info(
            "飞书业务请求成功 method=%s path=%s subject=%s http_status=%d "
            "request_id=%s duration_ms=%d response_fields=%s",
            method,
            path,
            token_context.subject_id,
            response.status_code,
            request_id,
            int((time.monotonic() - started_at) * 1000),
            sorted(data) if isinstance(data, dict) else [],
        )
        return data if isinstance(data, dict) else {}

    async def _primary_calendar_id(
        self,
        token_context: UserTokenContext,
        *,
        force: bool = False,
    ) -> str:
        """获取用户主日历 ID，使用 HTTP API 避免 SDK 返回错误的群组日历"""
        if not force and token_context.subject_id in self._primary_calendar_cache:
            return self._primary_calendar_cache[token_context.subject_id]
        self.logger.info(
            "开始获取用户主日历 subject=%s tenant=%s",
            token_context.subject_id,
            token_context.tenant_key,
        )
        async with httpx.AsyncClient(
            base_url=self.settings.domain,
            timeout=20,
            transport=self.http_transport,
            trust_env=False,
        ) as client:
            response = await client.get(
                "/open-apis/calendar/v4/calendars/primary",
                headers={
                    "Authorization": f"Bearer {token_context.access_token}",
                    "X-Tenant-Key": token_context.tenant_key,
                },
            )
        try:
            payload = response.json()
        except ValueError as error:
            self.logger.error(
                "获取用户主日历返回非 JSON subject=%s http_status=%d "
                "request_id=%s response_bytes=%d",
                token_context.subject_id,
                response.status_code,
                _response_request_id(response),
                len(response.content),
            )
            raise RuntimeError(f"飞书 OpenAPI 返回非 JSON 响应: HTTP {response.status_code}") from error
        self.logger.info(
            "获取用户主日历响应 subject=%s http_status=%d code=%s msg=%s "
            "request_id=%s",
            token_context.subject_id,
            response.status_code,
            payload.get("code"),
            payload.get("msg"),
            _response_request_id(response),
        )
        if response.is_error or payload.get("code"):
            raise FeishuApiError(
                "获取主日历失败: "
                f"HTTP {response.status_code} code={payload.get('code')} "
                f"msg={payload.get('msg')}",
                code=int(payload.get("code") or response.status_code),
                retryable=response.status_code == 429 or response.status_code >= 500,
                request_id=_response_request_id(response),
            )
        calendar_id = payload.get("data", {}).get("calendar_id")
        if not calendar_id:
            raise FeishuApiError(
                "飞书主日历接口返回成功但缺少 calendar_id",
                request_id=_response_request_id(response),
            )
        self._primary_calendar_cache[token_context.subject_id] = calendar_id
        self.logger.info("获取用户主日历成功: %s", calendar_id)
        return calendar_id

    async def _create_calendar_event_model(
        self,
        calendar_id: str,
        event: CalendarEvent,
        token_context: UserTokenContext,
        *,
        idempotency_key: str,
    ) -> tuple[str, str, str]:
        request = (
            CreateCalendarEventRequest.builder()
            .calendar_id(calendar_id)
            .idempotency_key(idempotency_key)
            .user_id_type("open_id")
            .request_body(event)
            .build()
        )
        self.logger.info(
            "创建日程请求 subject=%s calendar_id=%s summary=%s "
            "start=%s end=%s idempotency_key_prefix=%s",
            token_context.subject_id,
            calendar_id,
            event.summary,
            getattr(event.start_time, "timestamp", ""),
            getattr(event.end_time, "timestamp", ""),
            idempotency_key[:12],
        )
        started_at = time.monotonic()
        response = await asyncio.to_thread(
            self.client.calendar.v4.calendar_event.create,
            request,
            _request_option(token_context),
        )
        request_id = str(getattr(response, "request_id", "") or "")
        self.logger.info(
            "创建日程响应 subject=%s calendar_id=%s success=%s code=%s "
            "request_id=%s duration_ms=%d",
            token_context.subject_id,
            calendar_id,
            response.success(),
            getattr(response, "code", None),
            request_id,
            int((time.monotonic() - started_at) * 1000),
        )
        if not response.success():
            code = int(getattr(response, "code", 0) or 0)
            message = _translate_calendar_error(code, str(getattr(response, "msg", "")))
            retryable = code == 429 or 500 <= code < 600 or code in {99991400, 99991401}
            raise FeishuApiError(
                f"创建日程失败: {message}",
                code=code,
                retryable=retryable,
                request_id=request_id,
            )
        created = response.data.event if response.data else None
        if not created or not created.event_id:
            raise FeishuApiError(
                "飞书 OpenAPI 返回成功，但缺少 event_id",
                request_id=request_id,
            )
        return created.event_id, request_id, str(created.app_link or "")

    async def _create_event(
        self,
        calendar_id: str,
        record: DailyRecord,
        task: PlanTask,
        token_context: UserTokenContext,
        *,
        idempotency_key: str,
    ) -> str:
        event = _calendar_event(self.settings, record, task)
        start = getattr(getattr(event, "start_time", None), "timestamp", None)
        end = getattr(getattr(event, "end_time", None), "timestamp", None)
        self.logger.info(
            "创建单个日程请求 task=%s subject=%s calendar_id=%s summary=%s "
            "start_timestamp=%s end_timestamp=%s timezone=%s "
            "idempotency_key_prefix=%s",
            task.id,
            token_context.subject_id,
            calendar_id,
            getattr(event, "summary", ""),
            start,
            end,
            self.settings.timezone,
            idempotency_key[:12],
        )
        request = (
            CreateCalendarEventRequest.builder()
            .calendar_id(calendar_id)
            .idempotency_key(idempotency_key)
            .user_id_type("open_id")
            .request_body(event)
            .build()
        )
        response = await asyncio.to_thread(
            self.client.calendar.v4.calendar_event.create,
            request,
            _request_option(token_context),
        )
        self.logger.info(
            "创建单个日程响应 task=%s subject=%s success=%s code=%s msg=%s "
            "request_id=%s",
            task.id,
            token_context.subject_id,
            response.success(),
            getattr(response, "code", None),
            getattr(response, "msg", None),
            getattr(response, "request_id", None),
        )
        if not response.success():
            error_msg = _translate_calendar_error(response.code, response.msg)
            raise RuntimeError(f"创建日程失败: {error_msg}")
        event = response.data.event if response.data else None
        if not event or not event.event_id:
            raise RuntimeError("飞书 OpenAPI 返回成功，但缺少 event_id")
        return event.event_id


def _response_request_id(response: httpx.Response) -> str:
    return (
        response.headers.get("x-tt-logid")
        or response.headers.get("x-request-id")
        or response.headers.get("x-lark-request-id")
        or ""
    )


def _event_id_for_scope(event_id: str, scope: str) -> str:
    if scope == "single":
        return event_id
    event_uid, separator, _original_time = event_id.rpartition("_")
    return f"{event_uid}_0" if separator else event_id


def _task_time(value: datetime, is_all_day: bool) -> dict[str, Any]:
    return {
        "timestamp": str(int(value.timestamp() * 1000)),
        "is_all_day": is_all_day,
    }


def _task_result(data: dict[str, Any]) -> dict[str, Any]:
    task = data.get("task") if isinstance(data.get("task"), dict) else data
    guid = str(task.get("guid") or "")
    if not guid:
        raise FeishuApiError("飞书任务接口返回成功但缺少 guid")
    return {
        "task_guid": guid,
        "url": str(
            task.get("url")
            or f"https://applink.feishu.cn/client/todo/detail?guid={guid}"
        ),
        "task": task,
    }


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


def _calendar_event_from_command(command: CalendarCreateCommand) -> CalendarEvent:
    if command.start_time is None or command.end_time is None:
        raise ValueError("normalized calendar command requires start_time and end_time")
    builder = (
        CalendarEvent.builder()
        .summary(command.summary)
        .description(command.description or "")
        .start_time(
            TimeInfo.builder()
            .timestamp(str(int(command.start_time.timestamp())))
            .timezone(command.timezone)
            .build()
        )
        .end_time(
            TimeInfo.builder()
            .timestamp(str(int(command.end_time.timestamp())))
            .timezone(command.timezone)
            .build()
        )
        .reminders(
            [
                Reminder.builder().minutes(minutes).build()
                for minutes in command.reminder_minutes
            ]
        )
        .vchat(Vchat.builder().vc_type("no_meeting").build())
    )
    if command.recurrence:
        builder.recurrence(command.recurrence)
    if command.location:
        builder.location(EventLocation.builder().name(command.location).build())
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


def _translate_calendar_error(code: int, msg: str) -> str:
    """
    翻译飞书日历错误码为中文说明。

    Args:
        code: 飞书错误码
        msg: 原始错误消息

    Returns:
        中文错误说明
    """
    error_map = {
        191002: "当前调用身份无日历访问权限，请确认使用了已授权用户的访问令牌",
        191003: "日历不存在或已被删除",
        191004: "日历权限不足",
        191005: "日程时间冲突",
        191006: "日程不存在或已被删除",
        191007: "日程参与者数量超限",
        191008: "日程时间无效",
        191009: "重复日程规则无效",
        191010: "日程提醒时间无效",
        191011: "日程标题过长",
        191012: "日程描述过长",
        191013: "日程地点过长",
        191014: "日程参与者无效",
        191015: "日程组织者不能删除",
        191016: "日程已结束",
        191017: "日程已取消",
        191018: "日程已拒绝",
        191019: "日程已过期",
        191020: "日程已满",
        191021: "日程未开始",
        191022: "日程未结束",
        191023: "日程未取消",
        191024: "日程未拒绝",
        191025: "日程未过期",
        191026: "日程未满",
        191027: "日程未确认",
        191028: "日程未删除",
        191029: "日程未修改",
        191030: "日程未创建",
    }

    return error_map.get(code, f"{code} {msg}")
