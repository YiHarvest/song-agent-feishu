"""每个 LLM 选中工具的确定性策略门禁。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..agent.context import AgentContext
from ..agent.tool_registry import AgentTool


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    result: Literal["ALLOW", "BLOCK"]
    reason: str
    risk_level: Literal["low", "medium", "high"]


class ToolPolicyGuard:
    def evaluate(self, context: AgentContext, tool: AgentTool) -> PolicyDecision:
        del context
        if ".commit_" in tool.name or tool.name.startswith("oauth.commit"):
            return PolicyDecision("BLOCK", "LLM 不得调用内部提交工具", "high")
        if tool.category in {"read", "local", "prepare"}:
            risk = "medium" if tool.category == "prepare" else "low"
            return PolicyDecision("ALLOW", "工具在显式白名单内", risk)
        return PolicyDecision("BLOCK", "工具类别未获授权", "high")
