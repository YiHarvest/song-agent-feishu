"""LLM 可见工具的显式注册表。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .context import AgentContext
from .models import ToolResult

ToolHandler = Callable[[AgentContext, dict[str, Any]], Awaitable[ToolResult]]


@dataclass(frozen=True, slots=True)
class AgentTool:
    name: str
    description: str
    handler: ToolHandler = field(repr=False)
    arguments_schema: dict[str, Any] = field(default_factory=lambda: {"type": "object"})
    category: str = "local"

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.arguments_schema,
            "category": self.category,
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, AgentTool] = {}

    def register(self, tool: AgentTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name}")
        if ".commit_" in tool.name or tool.name.startswith("oauth.commit"):
            raise ValueError("internal commit tools cannot be LLM-visible")
        self._tools[tool.name] = tool

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self._tools.values()]

    def schemas_for(
        self,
        context: AgentContext | None = None,
        capabilities: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """
        根据上下文和能力需求动态裁剪工具 Schema。

        Args:
            context: Agent 上下文（可选）
            capabilities: 需要的能力集合（可选）

        Returns:
            裁剪后的工具 Schema 列表

        示例：
            - 计划请求：plans、reviews
            - 日历请求：calendar、plans
            - 文档请求：documents、websearch
            - 普通问答：不提供工具或只提供必要只读工具
        """
        # 如果没有指定能力，返回全部工具
        if not capabilities:
            return self.schemas()

        # 根据能力选择工具
        selected_tools = []

        for tool in self._tools.values():
            # 检查工具是否匹配所需能力
            if self._tool_matches_capabilities(tool, capabilities):
                selected_tools.append(tool.schema())

        return selected_tools

    def _tool_matches_capabilities(
        self,
        tool: AgentTool,
        capabilities: set[str],
    ) -> bool:
        """
        检查工具是否匹配所需能力。

        Args:
            tool: 工具对象
            capabilities: 能力集合

        Returns:
            如果匹配返回 True
        """
        # 工具名称到能力的映射
        tool_capabilities = {
            "calendar": {"calendar"},
            "plans": {"plans"},
            "reviews": {"plans"},
            "documents": {"documents"},
            "websearch": {"websearch"},
            "tool_results": {"websearch"},
            "user_preferences": {"preferences"},
            "ask_user": {"interaction"},
            "final_answer": {"answer"},
        }

        # 工具以 namespace.action 命名，能力映射按 namespace 维护。
        namespace = tool.name.partition(".")[0]
        tool_caps = tool_capabilities.get(namespace, set())

        # 如果工具的能力与所需能力有交集，则匹配
        return bool(tool_caps & capabilities)

    def get(self, name: str) -> AgentTool | None:
        return self._tools.get(name)

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        context: AgentContext,
    ) -> ToolResult:
        tool = self.get(name)
        if tool is None:
            return ToolResult(status="denied", summary="工具不存在或不可见")
        return await tool.handler(context, arguments)
