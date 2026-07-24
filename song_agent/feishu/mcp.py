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
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ..config import Settings
from ..models import UserTokenContext


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

    async def create_document(
        self, title: str, markdown: str, token_context: UserTokenContext
    ) -> CreatedDocument:
        tools = "docx.v1.document.create,docx.v1.documentBlockChildren.create"
        async with self._session(token_context.access_token, tools) as session:
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
        self,
        search_key: str,
        token_context: UserTokenContext,
        *,
        chat_id: str | None = None,
    ) -> list[FoundDocument]:
        async with self._session(token_context.access_token, "docx.builtin.search") as session:
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
        self,
        document_id: str,
        title: str,
        markdown: str,
        token_context: UserTokenContext,
    ) -> CreatedDocument:
        tools = "docx.v1.documentBlockChildren.get,docx.v1.documentBlockChildren.create"
        async with self._session(token_context.access_token, tools) as session:
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

    async def _call_tool(self, session: ClientSession, name: str, arguments: dict[str, Any]) -> Any:
        """调用 MCP 工具，输出可审计、无敏感信息的终端追踪日志。"""
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
    """如果未来工具参数包含凭据，则进行脱敏处理。"""
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
    """将 Markdown 转换为安全的文本块，无需 Drive 导入权限范围。"""
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
