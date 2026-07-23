"""
飞书 MCP 工具调用模块。

通过飞书官方 MCP (Model Context Protocol) 调用日历、文档等 API。
支持创建日程事件和云文档。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ..config import Settings
from ..models import DailyRecord, PlanTask


@dataclass
class CalendarCreationResult:
    created: list[tuple[str, str]] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


@dataclass
class CreatedDocument:
    title: str
    token: str
    url: str


@dataclass
class FoundDocument:
    title: str
    token: str
    url: str


class FeishuMcp:
    """
    飞书 MCP 工具调用器。

    通过飞书官方 MCP 调用日历、文档等 API，支持用户身份授权。
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.logger = logging.getLogger(__name__)

    async def create_events(
        self,
        record: DailyRecord,
        access_token: str,
        task_ids: set[str] | None = None,
    ) -> CalendarCreationResult:
        output = CalendarCreationResult()
        selected = [task for task in record.tasks if task_ids is None or task.id in task_ids]
        candidates = [task for task in selected if task.start_time and not task.calendar_event_id]
        output.skipped = [task.id for task in selected if not task.start_time]
        if not candidates:
            return output
        async with self._session(
            access_token, "preset.calendar.default,calendar.v4.calendarEvent.delete"
        ) as session:
            calendar_id = self.settings.feishu_calendar_id or await self._primary_calendar_id(session)
            for task in candidates:
                try:
                    event_id = await self._create_event(session, calendar_id, record, task)
                    output.created.append((task.id, event_id))
                except Exception as error:
                    output.failed.append((task.id, str(error)))
                    self.logger.exception("通过 MCP 创建飞书日程失败: %s", task.id)
        return output

    async def delete_event(self, event_id: str, access_token: str) -> None:
        async with self._session(
            access_token, "preset.calendar.default,calendar.v4.calendarEvent.delete"
        ) as session:
            calendar_id = self.settings.feishu_calendar_id or await self._primary_calendar_id(session)
            result = await self._call_tool(
                session,
                "calendar_v4_calendarEvent_delete",
                {"path": {"calendar_id": calendar_id, "event_id": event_id}, "useUAT": True},
            )
            parse_mcp_result(result)

    async def create_document(self, title: str, markdown: str, access_token: str) -> CreatedDocument:
        tools = "docx.v1.document.create,docx.v1.documentBlockChildren.create"
        async with self._session(access_token, tools) as session:
            created = await self._call_tool(
                session,
                "docx_v1_document_create",
                {"data": {"title": sanitize_title(title)}, "useUAT": True},
            )
            payload = parse_mcp_result(created)
            document_id = find_string(payload, "document_id")
            if not document_id:
                raise RuntimeError("MCP 返回成功，但缺少 document_id")

            blocks = markdown_to_text_blocks(markdown, title)
            for offset in range(0, len(blocks), 50):
                result = await self._call_tool(
                    session,
                    "docx_v1_documentBlockChildren_create",
                    {
                        "path": {"document_id": document_id, "block_id": document_id},
                        "data": {"children": blocks[offset : offset + 50], "index": offset},
                        "useUAT": True,
                    },
                )
                parse_mcp_result(result)
            return CreatedDocument(
                title=title,
                token=document_id,
                url=f"https://feishu.cn/docx/{document_id}",
            )

    async def search_documents(
        self, search_key: str, access_token: str, *, chat_id: str | None = None
    ) -> list[FoundDocument]:
        async with self._session(access_token, "docx.builtin.search") as session:
            data: dict[str, Any] = {"search_key": search_key, "count": 50}
            if chat_id:
                data["chat_ids"] = [chat_id]
            result = await self._call_tool(session, "docx_builtin_search", {"data": data, "useUAT": True})
            payload = parse_mcp_result(result)
        found: dict[str, FoundDocument] = {}
        for item in walk_dicts(payload):
            token = next(
                (
                    item.get(key)
                    for key in ("docs_token", "document_id", "obj_token", "token")
                    if isinstance(item.get(key), str)
                ),
                None,
            )
            title = next(
                (item.get(key) for key in ("title", "name") if isinstance(item.get(key), str)),
                None,
            )
            if token and title:
                url = next(
                    (item.get(key) for key in ("url", "docs_url") if isinstance(item.get(key), str)),
                    f"https://feishu.cn/docx/{token}",
                )
                found[token] = FoundDocument(title=title, token=token, url=url)
        return list(found.values())

    async def append_document(
        self, document_id: str, title: str, markdown: str, access_token: str
    ) -> CreatedDocument:
        tools = "docx.v1.documentBlockChildren.get,docx.v1.documentBlockChildren.create"
        async with self._session(access_token, tools) as session:
            index = 0
            page_token: str | None = None
            while True:
                params: dict[str, Any] = {"page_size": 500}
                if page_token:
                    params["page_token"] = page_token
                result = await self._call_tool(
                    session,
                    "docx_v1_documentBlockChildren_get",
                    {
                        "path": {"document_id": document_id, "block_id": document_id},
                        "params": params,
                        "useUAT": True,
                    },
                )
                payload = parse_mcp_result(result)
                items = payload.get("items", []) if isinstance(payload, dict) else []
                index += len(items) if isinstance(items, list) else 0
                if not isinstance(payload, dict) or not payload.get("has_more"):
                    break
                page_token = payload.get("page_token")
                if not isinstance(page_token, str) or not page_token:
                    break

            blocks = markdown_to_text_blocks(markdown, "")
            for offset in range(0, len(blocks), 50):
                result = await self._call_tool(
                    session,
                    "docx_v1_documentBlockChildren_create",
                    {
                        "path": {"document_id": document_id, "block_id": document_id},
                        "data": {"children": blocks[offset : offset + 50], "index": index + offset},
                        "useUAT": True,
                    },
                )
                parse_mcp_result(result)
        return CreatedDocument(title=title, token=document_id, url=f"https://feishu.cn/docx/{document_id}")

    @asynccontextmanager
    async def _session(self, access_token: str, tools: str) -> AsyncIterator[ClientSession]:
        cli = self.settings.resolve_mcp_cli()
        node = shutil.which("node")
        npx = shutil.which("npx")
        if cli and node:
            command = node
            prefix = [str(cli)]
        elif npx:
            command = npx
            prefix = ["-y", "@larksuiteoapi/lark-mcp@0.5.1"]
        else:
            raise RuntimeError("调用飞书官方 MCP 需要 Node.js 和 npx，或设置 FEISHU_MCP_CLI")
        server = StdioServerParameters(
            command=command,
            args=[
                *prefix,
                "mcp",
                "-a",
                self.settings.feishu_app_id,
                "-s",
                self.settings.feishu_app_secret,
                "-d",
                self.settings.domain,
                "-t",
                tools,
                "--token-mode",
                "user_access_token",
                "-u",
                access_token,
                "-l",
                "zh",
            ],
        )
        with open(os.devnull, "w", encoding="utf-8") as errlog:  # noqa: ASYNC230
            async with stdio_client(server, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session

    async def _primary_calendar_id(self, session: ClientSession) -> str:
        result = await self._call_tool(session, "calendar_v4_calendar_primary", {"useUAT": True})
        payload = parse_mcp_result(result)
        calendar_id = find_string(payload, "calendar_id")
        if not calendar_id:
            raise RuntimeError("MCP 未返回用户主日历 ID")
        return calendar_id

    async def _create_event(
        self, session: ClientSession, calendar_id: str, record: DailyRecord, task: PlanTask
    ) -> str:
        zone = ZoneInfo(self.settings.timezone)
        start = datetime.fromisoformat(f"{record.date}T{task.start_time}").replace(tzinfo=zone)
        end = (
            datetime.fromisoformat(f"{record.date}T{task.end_time}").replace(tzinfo=zone)
            if task.end_time
            else start + timedelta(minutes=30)
        )
        if end <= start:
            end += timedelta(days=1)

        event_data: dict[str, Any] = {
            "summary": f"[{task.id}] {task.title}",
            "description": f"由个人管家根据 {record.date} 日计划创建；创建者仅为当前授权用户。",
            "start_time": {
                "timestamp": str(int(start.timestamp())),
                "timezone": self.settings.timezone,
            },
            "end_time": {"timestamp": str(int(end.timestamp())), "timezone": self.settings.timezone},
            "reminders": [{"minutes": 10}],
            "vchat": {"vc_type": "no_meeting"},
        }

        # 添加周期性设置
        if task.repeat == "daily":
            event_data["repeat_interval"] = "FREQ=DAILY"
        elif task.repeat == "weekdays":
            event_data["repeat_interval"] = "FREQ=WEEKDAYS"
        elif task.repeat == "weekly":
            event_data["repeat_interval"] = "FREQ=WEEKLY"

        result = await self._call_tool(
            session,
            "calendar_v4_calendarEvent_create",
            {
                "path": {"calendar_id": calendar_id},
                "data": event_data,
                "useUAT": True,
            },
        )
        payload = parse_mcp_result(result)
        event_id = find_string(payload, "event_id")
        if not event_id:
            raise RuntimeError("MCP 返回成功，但缺少 event_id")
        return event_id

    async def _call_tool(self, session: ClientSession, name: str, arguments: dict[str, Any]) -> Any:
        """Call an MCP tool with an auditable, secret-free terminal trace."""
        safe_arguments = redact_sensitive(arguments)
        self.logger.info(
            "🔧 调用工具 name=%s arguments=%s",
            name,
            json.dumps(safe_arguments, ensure_ascii=False),
        )
        try:
            result = await session.call_tool(name, arguments)
        except Exception:
            self.logger.exception("❌ 工具调用异常 name=%s", name)
            raise
        self.logger.info(
            "%s 工具调用完成 name=%s",
            "❌" if getattr(result, "isError", False) else "✅",
            name,
        )
        return result


def parse_mcp_result(result: Any) -> Any:
    text = next((item.text for item in result.content if getattr(item, "type", None) == "text"), None)
    if getattr(result, "isError", False):
        raise RuntimeError(text or "飞书 MCP 调用失败")
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"text": text}


def redact_sensitive(value: Any) -> Any:
    """Redact credentials if future tool arguments ever contain them."""
    if isinstance(value, dict):
        return {
            key: (
                "***"
                if any(marker in key.lower() for marker in ("token", "secret", "authorization"))
                else redact_sensitive(child)
            )
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    return value


def find_string(value: Any, key: str) -> str | None:
    if isinstance(value, dict):
        if isinstance(value.get(key), str):
            return value[key]
        for child in value.values():
            found = find_string(child, key)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_string(child, key)
            if found:
                return found
    return None


def walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child)


def sanitize_title(title: str) -> str:
    value = re.sub(r'[\\/:*?"<>|]', " ", title)
    value = re.sub(r"\s+", " ", value).strip()
    return (value or "Agent 云文档")[:27]


def markdown_to_text_blocks(markdown: str, title: str) -> list[dict[str, Any]]:
    """Convert Markdown to safe text blocks without requiring the audited Drive import scope."""
    cleaned = markdown.replace("\r\n", "\n").replace("\r", "\n")
    paragraphs: list[str] = []
    for raw in re.split(r"\n\s*\n|\n", cleaned):
        value = raw.strip()
        if not value or value.startswith("```"):
            continue
        value = re.sub(r"^#{1,6}\s+", "", value)
        value = re.sub(r"^[-*+]\s+", "• ", value)
        value = re.sub(r"^\d+[.)]\s+", "", value)
        value = re.sub(r"(\*\*|__|`)", "", value).strip()
        if value == title and not paragraphs:
            continue
        paragraphs.extend(value[index : index + 1500] for index in range(0, len(value), 1500))
    if not paragraphs:
        paragraphs = ["待补充"]
    return [
        {
            "block_type": 2,
            "text": {"elements": [{"text_run": {"content": paragraph}}]},
        }
        for paragraph in paragraphs
    ]
