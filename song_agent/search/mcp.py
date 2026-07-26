"""search-engine-tool-mcp 客户端。"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ..config import Settings


class SearchMcpError(RuntimeError):
    """搜索 MCP 调用或协议错误。"""


@dataclass(frozen=True, slots=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    source: str


class SearchMcp:
    """通过已安装的 MCP server 执行只读网络搜索。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.logger = logging.getLogger(__name__)

    async def search(
        self,
        query: str,
        *,
        provider: str = "auto",
        max_results: int = 5,
        search_depth: str = "basic",
        include_answer: bool = False,
    ) -> list[SearchResult]:
        env = os.environ.copy()
        provider_settings = {
            "SEARXNG_BASE_URL": self.settings.searxng_base_url,
            "TALORDATA_API_KEY": self.settings.talordata_api_key,
            "YDC_API_KEY": self.settings.ydc_api_key,
            "TAVILY_API_KEY": self.settings.tavily_api_key,
        }
        env.update({name: value for name, value in provider_settings.items() if value})
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "search_engine_tool_mcp.server"],
            env=env,
        )
        try:
            async with stdio_client(params) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        "web_search",
                        arguments={
                            "query": query,
                            "provider": provider,
                            "max_results": max_results,
                            "search_depth": search_depth,
                            "include_answer": include_answer,
                        },
                    )
        except Exception as error:
            self.logger.exception(
                "搜索 MCP 调用失败 provider=%s query_chars=%d",
                provider,
                len(query),
            )
            raise SearchMcpError(str(error)) from error

        if result.isError:
            detail = " ".join(
                item.text for item in result.content if hasattr(item, "text")
            )
            raise SearchMcpError(detail or "web_search 返回错误")
        text_items = [
            item.text for item in result.content if hasattr(item, "text")
        ]
        if len(text_items) != 1:
            raise SearchMcpError("web_search 返回体不是单一 JSON 文本")
        try:
            payload: Any = json.loads(text_items[0])
        except json.JSONDecodeError as error:
            raise SearchMcpError("web_search 返回了非法 JSON") from error
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise SearchMcpError("web_search 返回体缺少 results")

        source = str(payload.get("provider") or provider)
        return [
            SearchResult(
                title=str(hit.get("title") or ""),
                url=str(hit.get("href") or ""),
                snippet=str(hit.get("abstract") or ""),
                source=str(hit.get("source") or source),
            )
            for hit in payload["results"]
            if isinstance(hit, dict)
        ]
