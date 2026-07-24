"""
Agent 运行预算管理。

实现 Agent Run 级别的时间预算、请求预算和工具调用预算，
确保复杂请求不会因单个慢 LLM 调用吃掉全部运行预算。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings


@dataclass
class AgentRunBudget:
    """
    Agent 运行预算。
    
    管理 Agent Run 级别的时间预算、请求预算和工具调用预算，
    确保复杂请求不会因单个慢 LLM 调用吃掉全部运行预算。
    
    Attributes:
        deadline: 截止时间（monotonic 时间戳）
        max_llm_requests: 最大 LLM 请求数
        max_tool_calls: 最大工具调用数
        max_steps: 最大步骤数
        max_input_tokens: 最大输入 Token（可选）
        max_output_tokens: 最大输出 Token（可选）
        llm_requests: 已使用的 LLM 请求数
        tool_calls: 已使用的工具调用数
        steps: 已使用的步骤数
    """
    
    deadline: float
    max_llm_requests: int
    max_tool_calls: int
    max_steps: int
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    
    llm_requests: int = field(default=0, init=False)
    tool_calls: int = field(default=0, init=False)
    steps: int = field(default=0, init=False)
    
    # 内部状态
    _consecutive_errors: int = field(default=0, init=False)
    _last_error: Exception | None = field(default=None, init=False)
    
    @classmethod
    def create(cls, settings: Settings) -> AgentRunBudget:
        """
        从配置创建预算实例。
        
        Args:
            settings: 应用配置
        
        Returns:
            预算实例
        """
        deadline = time.monotonic() + settings.agent_run_timeout_seconds
        return cls(
            deadline=deadline,
            max_llm_requests=settings.agent_max_llm_requests,
            max_tool_calls=settings.agent_max_tool_calls,
            max_steps=settings.agent_max_steps,
        )
    
    def remaining_seconds(self) -> float:
        """
        计算剩余时间（秒）。
        
        Returns:
            剩余时间（秒），如果已超时则为负数
        """
        return self.deadline - time.monotonic()
    
    def is_expired(self) -> bool:
        """
        检查预算是否已过期。
        
        Returns:
            如果已过期返回 True
        """
        return self.remaining_seconds() <= 0
    
    def ensure_can_call_llm(self, reserve_seconds: float = 10.0) -> None:
        """
        确保可以调用 LLM。
        
        Args:
            reserve_seconds: 预留时间（秒）
        
        Raises:
            BudgetExceededError: 如果预算已耗尽
        """
        # 检查时间预算
        remaining = self.remaining_seconds()
        if remaining <= reserve_seconds:
            raise BudgetExceededError(
                f"时间预算不足：剩余 {remaining:.1f} 秒，需要预留 {reserve_seconds} 秒"
            )
        
        # 检查请求预算
        if self.llm_requests >= self.max_llm_requests:
            raise BudgetExceededError(
                f"LLM 请求预算已耗尽：已使用 {self.llm_requests}/{self.max_llm_requests}"
            )
        
        # 检查步骤预算
        if self.steps >= self.max_steps:
            raise BudgetExceededError(
                f"步骤预算已耗尽：已使用 {self.steps}/{self.max_steps}"
            )
    
    def ensure_can_call_tool(self) -> None:
        """
        确保可以调用工具。
        
        Raises:
            BudgetExceededError: 如果预算已耗尽
        """
        # 检查工具调用预算
        if self.tool_calls >= self.max_tool_calls:
            raise BudgetExceededError(
                f"工具调用预算已耗尽：已使用 {self.tool_calls}/{self.max_tool_calls}"
            )
    
    def record_llm_request(self) -> None:
        """记录一次 LLM 请求"""
        self.llm_requests += 1
    
    def record_tool_call(self) -> None:
        """记录一次工具调用"""
        self.tool_calls += 1
    
    def record_step(self) -> None:
        """记录一个步骤"""
        self.steps += 1
    
    def record_error(self, error: Exception) -> None:
        """
        记录错误。
        
        Args:
            error: 错误对象
        """
        self._consecutive_errors += 1
        self._last_error = error
    
    def reset_error_count(self) -> None:
        """重置连续错误计数"""
        self._consecutive_errors = 0
        self._last_error = None
    
    def has_exceeded_max_errors(self, max_errors: int) -> bool:
        """
        检查是否超过最大连续错误数。
        
        Args:
            max_errors: 最大连续错误数
        
        Returns:
            如果超过返回 True
        """
        return self._consecutive_errors >= max_errors
    
    def calculate_step_timeout(self, settings: Settings) -> float:
        """
        计算单步超时时间。
        
        确保单步超时不会突破总 deadline。
        
        Args:
            settings: 应用配置
        
        Returns:
            单步超时时间（秒）
        """
        remaining = self.remaining_seconds()
        reserve = settings.agent_finish_reserve_seconds
        
        # 如果剩余时间不足，返回 0
        if remaining <= reserve:
            return 0.0
        
        # 取最小值，确保不会突破总 deadline
        step_timeout = min(
            settings.agent_step_timeout_seconds,
            remaining - reserve,
        )
        
        return max(0.0, step_timeout)
    
    def to_summary(self) -> dict[str, Any]:
        """
        生成预算摘要。
        
        Returns:
            预算摘要字典
        """
        return {
            "remaining_seconds": round(self.remaining_seconds(), 2),
            "llm_requests": f"{self.llm_requests}/{self.max_llm_requests}",
            "tool_calls": f"{self.tool_calls}/{self.max_tool_calls}",
            "steps": f"{self.steps}/{self.max_steps}",
            "consecutive_errors": self._consecutive_errors,
        }


class BudgetExceededError(Exception):
    """预算耗尽错误"""
    pass