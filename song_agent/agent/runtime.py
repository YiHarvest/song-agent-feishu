"""Bounded ReAct loop with deterministic tool and policy enforcement."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..config import Settings
from ..policies.tool_policy import ToolPolicyGuard
from .budget import AgentRunBudget, BudgetExceededError
from .context import AgentContext
from .context_builder import AgentContextBuilder, ContextConfig
from .models import AgentDecision, AgentResult, ToolResult
from .tool_registry import ToolRegistry

if TYPE_CHECKING:
    from ..services.agent_runs import AgentRunRecorder


@dataclass(frozen=True, slots=True)
class AgentLimits:
    """Agent 运行限制（已废弃，使用 AgentRunBudget）"""

    max_steps: int = 12
    max_tool_calls: int = 8
    max_consecutive_tool_errors: int = 2
    timeout_seconds: int = 90


class ReActRuntime:
    """
    ReAct 运行时。

    实现带预算管理的 ReAct 循环，支持：
    - 时间预算（deadline propagation）
    - 请求预算（LLM 请求数限制）
    - 工具调用预算
    - 上下文裁剪
    - 工具动态加载
    - 错误分类和重试
    """

    def __init__(
        self,
        llm,
        tools: ToolRegistry,
        policy: ToolPolicyGuard,
        limits: AgentLimits,
        recorder: AgentRunRecorder | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.llm = llm
        self.tools = tools
        self.policy = policy
        self.limits = limits
        self.recorder = recorder
        self.settings = settings
        self.logger = logging.getLogger(__name__)

        # 上下文构建器
        self.context_builder = AgentContextBuilder(ContextConfig())

    async def run(self, context: AgentContext) -> AgentResult:
        """
        运行 ReAct 循环。

        Args:
            context: Agent 上下文

        Returns:
            Agent 结果
        """
        # 创建预算
        budget = AgentRunBudget.create(self.settings) if self.settings else None

        # 记录开始时间
        started_at = time.monotonic()

        try:
            # 使用预算管理运行
            if budget:
                result = await self._run_with_budget(context, budget)
            else:
                # 兼容旧逻辑
                result = await asyncio.wait_for(
                    self._run_legacy(context),
                    timeout=self.limits.timeout_seconds,
                )

            # 记录总耗时
            duration_ms = int((time.monotonic() - started_at) * 1000)
            self.logger.info(
                "🤖 Agent Run 完成 run_id=%s status=%s steps=%d tools=%d duration_ms=%d",
                context.run_id,
                result.status,
                result.step_count,
                result.tool_call_count,
                duration_ms,
            )

            return result

        except TimeoutError:
            # 总超时
            duration_ms = int((time.monotonic() - started_at) * 1000)
            self.logger.error(
                "🤖 Agent Run 超时 run_id=%s duration_ms=%d",
                context.run_id,
                duration_ms,
            )

            return AgentResult(
                status="failed",
                response=self._format_timeout_response(budget),
                step_count=budget.steps if budget else 0,
                tool_call_count=budget.tool_calls if budget else 0,
                error_code="agent_timeout",
            )

        except BudgetExceededError as e:
            # 预算耗尽
            duration_ms = int((time.monotonic() - started_at) * 1000)
            self.logger.error(
                "🤖 Agent Run 预算耗尽 run_id=%s reason=%s duration_ms=%d",
                context.run_id,
                str(e),
                duration_ms,
            )

            return AgentResult(
                status="failed",
                response=f"任务预算耗尽：{e}",
                step_count=budget.steps if budget else 0,
                tool_call_count=budget.tool_calls if budget else 0,
                error_code="budget_exceeded",
            )

    async def _run_with_budget(
        self,
        context: AgentContext,
        budget: AgentRunBudget,
    ) -> AgentResult:
        """带预算管理的运行"""
        observations: list[dict[str, str]] = []
        seen_calls: set[str] = set()
        consecutive_errors = 0

        for step_index in range(budget.max_steps):
            # 检查预算
            try:
                budget.ensure_can_call_llm(reserve_seconds=self.settings.agent_finish_reserve_seconds)
            except BudgetExceededError:
                return self._create_budget_exceeded_result(budget)

            # 记录步骤
            budget.record_step()

            # 计算单步超时
            step_timeout = budget.calculate_step_timeout(self.settings)
            if step_timeout <= 0:
                return AgentResult(
                    status="failed",
                    response=self._format_timeout_response(budget),
                    step_count=step_index,
                    tool_call_count=budget.tool_calls,
                    error_code="agent_timeout",
                )

            # 执行决策（带超时）
            try:
                async with asyncio.timeout(step_timeout):
                    decision = await self._make_decision(
                        context,
                        observations,
                        step_index,
                        budget,
                    )
            except TimeoutError:
                # 单步超时
                self.logger.warning(
                    "🤖 Agent Step 超时 run_id=%s step=%d timeout=%.1fs",
                    context.run_id,
                    step_index,
                    step_timeout,
                )

                # 检查是否可以重试
                if budget.remaining_seconds() > self.settings.agent_finish_reserve_seconds:
                    # 还有时间，继续下一步
                    observations.append(
                        {
                            "tool": "llm",
                            "status": "timeout",
                            "summary": f"LLM 决策超时（{step_timeout:.1f}秒）",
                        }
                    )
                    continue
                else:
                    # 没有时间了，返回超时结果
                    return AgentResult(
                        status="failed",
                        response=self._format_timeout_response(budget),
                        step_count=step_index,
                        tool_call_count=budget.tool_calls,
                        error_code="agent_timeout",
                    )

            # 记录 LLM 请求
            budget.record_llm_request()

            # 处理决策
            result = await self._process_decision(
                context,
                decision,
                observations,
                seen_calls,
                budget,
                step_index,
                consecutive_errors,
            )

            if result:
                return result

            # 更新连续错误计数
            if observations and observations[-1].get("status") == "error":
                consecutive_errors += 1
                budget.record_error(Exception(observations[-1].get("summary", "Unknown error")))
            else:
                consecutive_errors = 0
                budget.reset_error_count()

            # 检查连续错误
            if consecutive_errors >= self.settings.agent_max_consecutive_errors:
                return AgentResult(
                    status="failed",
                    response=f"连续 {consecutive_errors} 次工具执行失败，请检查请求或稍后重试。",
                    step_count=step_index + 1,
                    tool_call_count=budget.tool_calls,
                    error_code="consecutive_tool_errors",
                )

        # 步骤耗尽
        return AgentResult(
            status="failed",
            response="已达到最大步骤数，任务未能完成。请尝试简化请求。",
            step_count=budget.max_steps,
            tool_call_count=budget.tool_calls,
            error_code="agent_step_limit_exceeded",
        )

    async def _make_decision(
        self,
        context: AgentContext,
        observations: list[dict[str, str]],
        step_index: int,
        budget: AgentRunBudget,
    ) -> AgentDecision:
        """
        执行 LLM 决策。

        Args:
            context: Agent 上下文
            observations: 观察列表
            step_index: 步骤索引
            budget: 运行预算

        Returns:
            Agent 决策
        """
        # 动态裁剪工具 Schema
        capabilities = self._infer_capabilities(context, observations)
        tool_schemas = self.tools.schemas_for(context, capabilities)

        # 构建系统提示词（裁剪）
        system_prompt = self.context_builder.build_system_prompt(
            context,
            include_tools=True,
            tool_schemas=tool_schemas,
        )

        # 构建用户消息（裁剪）
        user_message = self.context_builder.build_user_message(
            context,
            include_history=True,
            include_documents="documents" in capabilities,
        )

        # 添加观察摘要
        if observations:
            observation_summary = self.context_builder.build_tool_result_summary(
                observations,
                max_results=5,
            )
            user_message += f"\n\n{observation_summary}"

        # 调用 LLM
        decision = await self.llm.generate(
            AgentDecision,
            system_prompt,
            user_message,
            run_id=context.run_id,
            step_index=step_index,
            max_tokens=self.settings.llm_decision_max_tokens,
            tool_schema_count=len(tool_schemas),
        )

        return _normalize_tool_decision(context, decision, tool_schemas)

    def _infer_capabilities(
        self,
        context: AgentContext,
        observations: list[dict[str, str]],
    ) -> set[str]:
        """
        推断所需能力。

        Args:
            context: Agent 上下文
            observations: 观察列表

        Returns:
            能力集合
        """
        capabilities = set()

        # 根据用户输入推断
        user_text = context.user_text.lower()

        if any(kw in user_text for kw in ["计划", "规划", "安排", "待办", "todo"]):
            capabilities.add("plans")

        if any(kw in user_text for kw in ["文档", "记录", "document"]):
            capabilities.add("documents")

        if any(
            kw in user_text
            for kw in [
                "搜索",
                "查找",
                "search",
                "查询",
                "检索",
                "天气",
                "气温",
                "下雨",
                "降雨",
                "空气质量",
                "weather",
                "forecast",
            ]
        ):
            capabilities.add("websearch")

        if any(kw in user_text for kw in ["偏好", "设置", "preference"]):
            capabilities.add("preferences")

        # 根据观察推断
        for obs in observations:
            tool = obs.get("tool", "")
            if "plan" in tool:
                capabilities.add("plans")
            elif "document" in tool:
                capabilities.add("documents")
            elif "websearch" in tool:
                capabilities.add("websearch")

        # 如果没有推断出任何能力，添加基础能力
        if not capabilities:
            capabilities.add("answer")

        return capabilities

    async def _process_decision(
        self,
        context: AgentContext,
        decision: AgentDecision,
        observations: list[dict[str, str]],
        seen_calls: set[str],
        budget: AgentRunBudget,
        step_index: int,
        consecutive_errors: int,
    ) -> AgentResult | None:
        """
        处理决策。

        Args:
            context: Agent 上下文
            decision: Agent 决策
            observations: 观察列表
            seen_calls: 已见调用集合
            budget: 运行预算
            step_index: 步骤索引
            consecutive_errors: 连续错误计数

        Returns:
            如果决策导致终止，返回 AgentResult；否则返回 None
        """
        # 记录决策
        if self.recorder is not None:
            await self.recorder.record_decision(context, step_index, decision)

        # 处理 final_answer
        if decision.type == "final_answer":
            if self.recorder is not None:
                await self.recorder.record_result(
                    context,
                    step_index,
                    ToolResult(status="ok", summary="final_answer"),
                )
            return AgentResult(
                status="completed",
                response=decision.content,
                step_count=step_index + 1,
                tool_call_count=budget.tool_calls,
            )

        # 处理 ask_user
        if decision.type == "ask_user":
            if self.recorder is not None:
                await self.recorder.record_result(
                    context,
                    step_index,
                    ToolResult(status="ok", summary="ask_user"),
                )
            return AgentResult(
                status="awaiting_user",
                response=decision.content,
                step_count=step_index + 1,
                tool_call_count=budget.tool_calls,
            )

        # 检查工具调用预算。先检查再计数，确保配置为 N 时允许实际执行 N 次。
        try:
            budget.ensure_can_call_tool()
        except BudgetExceededError:
            return AgentResult(
                status="failed",
                response="已达到最大工具调用数，任务未能完成。",
                step_count=step_index + 1,
                tool_call_count=budget.tool_calls,
                error_code="agent_tool_limit_exceeded",
            )
        budget.record_tool_call()

        # 检查重复调用
        fingerprint = _call_fingerprint(decision.tool_name, decision.arguments)
        if fingerprint in seen_calls:
            return AgentResult(
                status="failed",
                response="检测到重复的工具调用，任务终止。",
                step_count=step_index + 1,
                tool_call_count=budget.tool_calls,
                error_code="repeated_tool_call",
            )
        seen_calls.add(fingerprint)

        # 执行工具
        tool = self.tools.get(decision.tool_name)
        if tool is None:
            result = ToolResult(status="denied", summary="工具不存在或不可见")
        else:
            policy = self.policy.evaluate(context, tool)
            if policy.result == "BLOCK":
                result = ToolResult(status="denied", summary=policy.reason)
            else:
                try:
                    result = await self.tools.execute(
                        decision.tool_name,
                        decision.arguments,
                        context,
                    )
                except Exception as e:
                    self.logger.error(
                        "🤖 工具执行失败 run_id=%s tool=%s error=%s",
                        context.run_id,
                        decision.tool_name,
                        e,
                    )
                    result = ToolResult(status="error", summary=f"工具执行失败: {e}")

        # 记录结果
        if self.recorder is not None:
            await self.recorder.record_result(context, step_index, result)

        # 检查终止结果
        if result.terminal:
            return AgentResult(
                status="completed" if result.status == "ok" else "failed",
                response=result.response,
                step_count=step_index + 1,
                tool_call_count=budget.tool_calls,
                error_code="" if result.status == "ok" else "terminal_tool_failed",
            )

        # 添加观察
        observations.append(
            {
                "tool": decision.tool_name,
                "status": result.status,
                "summary": result.summary,
            }
        )

        return None

    def _create_budget_exceeded_result(self, budget: AgentRunBudget) -> AgentResult:
        """创建预算耗尽结果"""
        return AgentResult(
            status="failed",
            response=f"任务预算耗尽。已完成 {budget.steps} 步，{budget.tool_calls} 次工具调用。",
            step_count=budget.steps,
            tool_call_count=budget.tool_calls,
            error_code="budget_exceeded",
        )

    def _format_timeout_response(self, budget: AgentRunBudget | None) -> str:
        """格式化超时响应"""
        if not budget:
            return "处理超时，请缩小任务范围后重试。"

        parts = ["这次模型响应时间过长，任务尚未完成。", ""]

        # 已完成部分
        completed = []
        if budget.steps > 0:
            completed.append(f"- 已完成 {budget.steps} 个决策步骤")
        if budget.tool_calls > 0:
            completed.append(f"- 已执行 {budget.tool_calls} 次工具调用")

        if completed:
            parts.append("已完成：")
            parts.extend(completed)
        else:
            parts.append("已完成：")
            parts.append("- 已识别用户请求")

        # 未执行部分
        parts.append("")
        parts.append("未执行：")
        parts.append("- 尚未创建任何日程或文档")
        parts.append("- 尚未执行任何外部写操作")

        parts.append("")
        parts.append("可以重新尝试当前请求。")

        return "\n".join(parts)

    async def _run_legacy(self, context: AgentContext) -> AgentResult:
        """兼容旧的运行逻辑"""
        observations: list[dict[str, str]] = []
        seen_calls: set[str] = set()
        tool_calls = 0
        consecutive_errors = 0
        for step_index in range(self.limits.max_steps):
            decision = await self.llm.generate(
                AgentDecision,
                _system_prompt(self.tools),
                _user_prompt(context, observations),
            )
            decision = _normalize_tool_decision(
                context,
                decision,
                self.tools.schemas(),
            )
            if self.recorder is not None:
                await self.recorder.record_decision(context, step_index, decision)
            if decision.type == "final_answer":
                if self.recorder is not None:
                    await self.recorder.record_result(
                        context,
                        step_index,
                        ToolResult(status="ok", summary="final_answer"),
                    )
                return AgentResult(
                    status="completed",
                    response=decision.content,
                    step_count=step_index + 1,
                    tool_call_count=tool_calls,
                )
            if decision.type == "ask_user":
                if self.recorder is not None:
                    await self.recorder.record_result(
                        context,
                        step_index,
                        ToolResult(status="ok", summary="ask_user"),
                    )
                return AgentResult(
                    status="awaiting_user",
                    response=decision.content,
                    step_count=step_index + 1,
                    tool_call_count=tool_calls,
                )
            tool_calls += 1
            if tool_calls > self.limits.max_tool_calls:
                return _failed(step_index + 1, tool_calls, "agent_tool_limit_exceeded")
            fingerprint = _call_fingerprint(decision.tool_name, decision.arguments)
            if fingerprint in seen_calls:
                return _failed(step_index + 1, tool_calls, "repeated_tool_call")
            seen_calls.add(fingerprint)
            tool = self.tools.get(decision.tool_name)
            if tool is None:
                result = ToolResult(status="denied", summary="工具不存在或不可见")
            else:
                policy = self.policy.evaluate(context, tool)
                if policy.result == "BLOCK":
                    result = ToolResult(status="denied", summary=policy.reason)
                else:
                    try:
                        result = await self.tools.execute(
                            decision.tool_name,
                            decision.arguments,
                            context,
                        )
                    except Exception:
                        result = ToolResult(status="error", summary="工具执行失败")
            if result.terminal:
                if self.recorder is not None:
                    await self.recorder.record_result(context, step_index, result)
                return AgentResult(
                    status="completed" if result.status == "ok" else "failed",
                    response=result.response,
                    step_count=step_index + 1,
                    tool_call_count=tool_calls,
                    error_code="" if result.status == "ok" else "terminal_tool_failed",
                )
            observations.append(
                {
                    "tool": decision.tool_name,
                    "status": result.status,
                    "summary": result.summary,
                }
            )
            if self.recorder is not None:
                await self.recorder.record_result(context, step_index, result)
            consecutive_errors = consecutive_errors + 1 if result.status == "error" else 0
            if consecutive_errors >= self.limits.max_consecutive_tool_errors:
                return _failed(step_index + 1, tool_calls, "consecutive_tool_errors")
        return _failed(self.limits.max_steps, tool_calls, "agent_step_limit_exceeded")


def _system_prompt(registry: ToolRegistry) -> str:
    return "\n".join(
        (
            "你是宋管家的 ReAct 决策器。只输出 JSON，不输出隐藏推理。",
            "每一步只能选择 final_answer、ask_user 或 tool_call。",
            "",
            "## 重要优化原则",
            "1. **优先使用 final_answer**：对于简单问题、问候、说明、状态查询，直接回答，不要调用工具。",
            "2. **避免不必要的工具调用**：只有在需要真实操作时才调用工具。",
            "3. **一次性完成**：调用工具时必须在本次 decision 的 arguments 中"
            "一次性给出工具 schema 要求的完整参数。",
            "4. **信息不足时 ask_user**：不要猜测或虚构参数。",
            "",
            "## 工具使用规则",
            "- 外部写入只能调用 prepare 工具；commit 工具不可见，也不得猜测。",
            "- 计划、复盘和文档正文都由本次 decision 生成；工具不会再次调用模型补全。",
            "- 普通问候、说明和无需工具的问题直接使用 final_answer，不调用工具。",
            "",
            "## 业务规则",
            "- 计划只拆分用户明确提到的事项：A=关键必做，B=重要，C=辅助生活；",
            "  未明确时间必须为 null，相对时间依据 current_time，周期词映射 repeat。",
            "- 复盘只能依据明确反馈；today_tasks 中未提到的任务标记 unconfirmed。",
            "- 文档不得虚构事实、数据、人物或引用；信息不足时明确标记待补充。",
            "",
            "可用工具：",
            json.dumps(registry.schemas(), ensure_ascii=False, separators=(",", ":")),
            "",
            '格式：{"type":"tool_call","tool_name":"...","arguments":{},'
            '"content":"","decision_summary":"..."}',
        )
    )


def _user_prompt(
    context: AgentContext,
    observations: list[dict[str, str]],
) -> str:
    return json.dumps(
        {
            "user_message": context.user_text,
            "conversation": context.conversation_key,
            "state_summary": context.metadata.get("state_summary", ""),
            "current_time": context.metadata.get("current_time", ""),
            "today_tasks": context.metadata.get("today_tasks", []),
            "observations": observations,
        },
        ensure_ascii=False,
    )


def _call_fingerprint(tool_name: str, arguments: dict) -> str:
    canonical = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{tool_name}:{canonical}".encode()).hexdigest()


def _failed(step_count: int, tool_calls: int, code: str) -> AgentResult:
    return AgentResult(
        status="failed",
        response="当前任务未能安全完成，请调整要求后重试。",
        step_count=step_count,
        tool_call_count=tool_calls,
        error_code=code,
    )


def _normalize_tool_decision(
    context: AgentContext,
    decision: AgentDecision,
    tool_schemas: list[dict],
) -> AgentDecision:
    if decision.type != "tool_call":
        return decision
    visible_tools = {str(schema.get("name") or "") for schema in tool_schemas}
    if decision.tool_name in visible_tools:
        return decision
    weather_terms = ("天气", "气温", "下雨", "降雨", "空气质量", "weather", "forecast")
    if (
        "websearch.search" in visible_tools
        and any(term in context.user_text.lower() for term in weather_terms)
    ):
        return decision.model_copy(
            update={
                "tool_name": "websearch.search",
                "arguments": {
                    "query": context.user_text,
                    "provider": "auto",
                    "max_results": 5,
                },
                "decision_summary": "搜索天气信息",
            }
        )
    return decision
