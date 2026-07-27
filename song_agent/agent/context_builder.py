"""
Agent 上下文构建器。

实现 Prompt 和上下文裁剪，减少每次输入模型的数据，
提升复杂请求的响应速度。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .context import AgentContext


@dataclass
class ContextConfig:
    """上下文配置"""

    max_history_messages: int = 10
    max_tool_results: int = 5
    max_document_chars: int = 2000
    max_plan_chars: int = 500
    max_preference_chars: int = 300


class AgentContextBuilder:
    """
    Agent 上下文构建器。

    实现 Prompt 和上下文裁剪，每次 LLM 调用只传：
    - 系统提示词
    - 最近必要对话
    - 当前任务摘要
    - 必要用户偏好
    - 当前步骤需要的工具
    - 工具结果摘要

    禁止默认传入：
    - 完整历史会话
    - 全部长期记忆
    - 全部计划记录
    - 全部工具 Schema
    - 完整文档正文
    - 所有历史工具结果
    """

    def __init__(self, config: ContextConfig | None = None) -> None:
        self.config = config or ContextConfig()
        self.logger = logging.getLogger(__name__)

    def build_system_prompt(
        self,
        context: AgentContext,
        *,
        include_tools: bool = True,
        tool_schemas: list[dict] | None = None,
    ) -> str:
        """
        构建系统提示词。

        Args:
            context: Agent 上下文
            include_tools: 是否包含工具 Schema
            tool_schemas: 工具 Schema 列表（如果为 None，使用全部工具）

        Returns:
            系统提示词
        """
        # 基础系统提示词（压缩版）
        parts = [
            "你是宋管家的 ReAct 决策器。只输出 JSON。",
            "每步选择：final_answer（直接回答）、ask_user（询问）、tool_call（调用工具）。",
            "",
            "## 核心规则",
            "- 需真实操作必须调用工具，禁止声称已执行未调用的操作",
            "- 只能调用“可用工具”中列出的精确工具名，禁止自造工具名",
            "- 工具参数必须一次性完整给出",
            "- 普通问候/说明直接 final_answer",
            "",
            "## 其他业务规则",
            "- 计划：A=关键必做，B=重要，C=辅助生活；任务名使用 title，不得使用 name",
            "- 计划时间只用 HH:MM；未明确时间=null；有开始时间但无时长时补合理结束时间",
            "- 复盘：只依据明确反馈，未提到任务标记 unconfirmed",
            "- 文档：不得虚构，信息不足标记待补充",
            "- 文档格式：日期标题必须包含具体时间，格式为 `## YYYY-MM-DD HH:MM`",
            "",
        ]

        # 添加状态摘要（从 metadata 获取）
        state_summary = context.metadata.get("state_summary", "")
        if state_summary:
            parts.append(f"状态：{state_summary}")

        # 添加当前时间
        current_time = context.metadata.get("current_time", "")
        if current_time:
            parts.append(f"时间：{current_time}")

        # 添加今日任务摘要（从 metadata 获取）
        today_tasks = context.metadata.get("today_tasks", [])
        if today_tasks:
            task_text = self._truncate_text(
                "\n".join(
                    f"- {t.get('id', '?')}: {t.get('title', '')} ({t.get('status', '')})"
                    for t in today_tasks[:5]
                ),
                self.config.max_plan_chars,
                "今日任务",
            )
            parts.append(f"今日任务：\n{task_text}")

        # 添加工具 Schema（简化格式）
        if include_tools and tool_schemas:
            parts.append("")
            parts.append("可用工具：")
            parts.append(_format_tool_schemas_compact(tool_schemas))

        # 添加输出格式说明（压缩版）
        parts.append("")
        parts.append("输出格式：")
        parts.append(
            '回答：{"type":"final_answer","content":"内容","tool_name":"","arguments":{},"decision_summary":"摘要"}'
        )
        parts.append(
            '询问：{"type":"ask_user","content":"问题","tool_name":"","arguments":{},"decision_summary":"摘要"}'
        )
        parts.append(
            '工具：{"type":"tool_call","content":"","tool_name":"工具名","arguments":{参数},"decision_summary":"摘要"}'
        )

        return "\n".join(parts)

    def build_user_message(
        self,
        context: AgentContext,
        *,
        include_history: bool = True,
        include_documents: bool = False,
    ) -> str:
        """
        构建用户消息。

        Args:
            context: Agent 上下文
            include_history: 是否包含历史对话
            include_documents: 是否包含文档内容

        Returns:
            用户消息
        """
        parts = []

        # 添加用户当前输入
        parts.append(f"用户输入：{context.user_text}")

        if include_history:
            history = context.metadata.get("conversation_context", [])
            formatted = self._format_history(history, self.config.max_history_messages)
            if formatted:
                parts.append(f"最近对话：\n{formatted}")

        summary = context.metadata.get("summary_context")
        if summary:
            parts.append(f"历史摘要：{summary}")

        memories = context.metadata.get("memory_context")
        if memories:
            parts.append(f"长期记忆：{memories}")

        retrieved = context.metadata.get("retrieved_context")
        if retrieved:
            parts.append(f"本次检索上下文：{retrieved}")

        # 添加对话键
        if context.conversation_key:
            parts.append(f"会话：{context.conversation_key}")

        return "\n".join(parts)

    def build_tool_result_summary(
        self,
        tool_results: list[dict],
        *,
        max_results: int | None = None,
    ) -> str:
        """
        构建工具结果摘要。

        Args:
            tool_results: 工具结果列表
            max_results: 最大结果数

        Returns:
            工具结果摘要
        """
        if not tool_results:
            return ""

        max_results = max_results or self.config.max_tool_results
        recent_results = tool_results[-max_results:]

        parts = ["## 工具结果摘要\n"]
        for i, result in enumerate(recent_results, 1):
            tool_name = result.get("tool", "unknown")
            summary = self._summarize_tool_result(result)
            parts.append(f"{i}. {tool_name}: {summary}")

        return "\n".join(parts)

    def _truncate_text(
        self,
        text: str,
        max_chars: int,
        label: str = "文本",
    ) -> str:
        """
        裁剪文本。

        Args:
            text: 原始文本
            max_chars: 最大字符数
            label: 标签（用于日志）

        Returns:
            裁剪后的文本
        """
        if len(text) <= max_chars:
            return text

        truncated = text[:max_chars]
        self.logger.debug(
            "%s 已裁剪：原始 %d 字符 -> 裁剪后 %d 字符",
            label,
            len(text),
            max_chars,
        )

        return truncated + "\n...（已裁剪）"

    def _format_history(
        self,
        history: list[dict],
        max_messages: int,
    ) -> str:
        """
        格式化历史对话。

        Args:
            history: 历史对话列表
            max_messages: 最大消息数

        Returns:
            格式化后的历史对话
        """
        if not history:
            return ""

        recent = history[-max_messages:]
        parts = []

        for msg in recent:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            # 裁剪每条消息
            if len(content) > 200:
                content = content[:200] + "..."
            parts.append(f"**{role}**: {content}")

        return "\n\n".join(parts)

    def _format_tool_schema(self, schema: dict) -> str:
        """
        格式化工具 Schema。

        Args:
            schema: 工具 Schema

        Returns:
            格式化后的工具描述
        """
        name = schema.get("name", "unknown")
        description = schema.get("description", "")
        parameters = schema.get("parameters", {})

        # 简化参数描述
        param_desc = ""
        if isinstance(parameters, dict):
            props = parameters.get("properties", {})
            if props:
                param_list = [f"`{k}`" for k in props.keys()]
                param_desc = f"参数：{', '.join(param_list)}"

        return f"### {name}\n\n{description}\n\n{param_desc}"

    def _summarize_tool_result(self, result: dict) -> str:
        """
        摘要工具结果。

        Args:
            result: 工具结果

        Returns:
            结果摘要
        """
        status = str(result.get("status") or "")
        summary = result.get("summary")
        if summary:
            text = str(summary)
            if len(text) > 200:
                text = text[:200] + "..."
            label = {
                "ok": "成功",
                "error": "错误",
                "denied": "拒绝",
            }.get(status, status or "结果")
            return f"{label}：{text}"

        output = result.get("output", "")
        error = result.get("error")

        if error:
            return f"错误：{str(error)[:100]}"

        if isinstance(output, str):
            if len(output) > 100:
                return output[:100] + "..."
            return output

        if isinstance(output, dict):
            return f"返回 {len(output)} 个字段"

        if isinstance(output, list):
            return f"返回 {len(output)} 个项目"

        return "完成"


def _format_tool_schemas_compact(schemas: list[dict]) -> str:
    """
    将工具 Schema 列表格式化为紧凑 JSON，保留所有嵌套约束。

    格式示例：
    plans.save_draft: 根据用户消息保存计划草稿
      参数 JSON Schema：{"type":"object",...}
    """
    parts = []
    for schema in schemas:
        name = schema.get("name", "unknown")
        desc = schema.get("description", "")
        params = schema.get("parameters", {})

        # 工具名称和描述
        parts.append(f"{name}: {desc}")

        if isinstance(params, dict):
            serialized = json.dumps(
                params,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            parts.append(f"  参数 JSON Schema：{serialized}")

        parts.append("")

    return "\n".join(parts)


def _format_tool_schemas_json(schemas: list[dict]) -> str:
    """将工具 Schema 列表格式化为 JSON 字符串（已废弃，使用 _format_tool_schemas_compact）"""
    return json.dumps(schemas, ensure_ascii=False, separators=(",", ":"))
