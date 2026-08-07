"""Agent 输入构造器（Q11）。

消费 Application 的 `BusinessContext`，构造传给开放 Agent 的运行时元数据。
属于 Application 层（它理解用户记忆、会话摘要和待确认 Action）；
`core/agent` 不应 import 本模块。
"""

from __future__ import annotations

from typing import Any

from ...context.models import BusinessContext, ContextBudget


class AgentInputBuilder:
    """按预算把六层上下文压成开放 Agent 的运行时元数据。"""

    def __init__(self, budget: ContextBudget | None = None) -> None:
        self.budget = budget or ContextBudget()

    def build_metadata(self, context: BusinessContext) -> dict[str, Any]:
        return _fit_budget(self._sections(context), self.budget.available_tokens)

    def _sections(self, context: BusinessContext) -> dict[str, Any]:
        recent = [
            {"role": message.role, "content": message.content}
            for message in context.recent_messages
        ]
        summary = (
            context.conversation_summary.model_dump(mode="json")
            if context.conversation_summary
            else {}
        )
        memories = [memory.model_dump(mode="json") for memory in context.memories]
        return {
            "request_context": context.request.model_dump(mode="json"),
            "business_context": {
                "active_pending_action": context.active_pending_action,
            },
            "conversation_context": recent,
            "summary_context": summary,
            "memory_context": memories,
            "retrieved_context": context.retrieved,
        }


def _fit_budget(sections: dict, max_tokens: int) -> dict:
    """保留必需层，按低优先级裁剪可选列表。"""

    def estimate(value: object) -> int:
        return max(1, len(str(value)) // 3)

    while estimate(sections) > max_tokens:
        if sections["retrieved_context"]:
            sections["retrieved_context"] = {}
        elif sections["memory_context"]:
            sections["memory_context"].pop()
        elif len(sections["conversation_context"]) > 4:
            sections["conversation_context"].pop(0)
        else:
            break
    return sections
