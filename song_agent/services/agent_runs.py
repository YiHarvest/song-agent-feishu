"""简洁的 Agent 运行和步骤历史持久化。"""

from __future__ import annotations

import hashlib
import json
import uuid

from ..agent.context import AgentContext
from ..agent.models import AgentDecision, AgentResult, ToolResult
from ..store import SqliteStore


class AgentRunRecorder:
    def __init__(self, store: SqliteStore, model: str) -> None:
        self.store = store
        self.model = model

    async def start(self, context: AgentContext) -> str:
        run_id = str(uuid.uuid4())
        message = context.message
        await self.store.start_agent_run(
            run_id=run_id,
            tenant_key=message.tenant_key,
            app_id=message.app_id,
            principal_id=message.user_id,
            conversation_key=context.conversation_key,
            inbound_message_id=message.message_id,
            model=self.model,
        )
        context.metadata["agent_run_id"] = run_id
        return run_id

    async def record_decision(
        self,
        context: AgentContext,
        step_index: int,
        decision: AgentDecision,
    ) -> None:
        run_id = str(context.metadata.get("agent_run_id") or "")
        if not run_id:
            return
        canonical = json.dumps(
            decision.arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        argument_shape = {
            key: type(value).__name__
            for key, value in sorted(decision.arguments.items())
        }
        await self.store.record_agent_step(
            run_id=run_id,
            step_index=step_index,
            decision_type=decision.type,
            decision_summary=decision.decision_summary,
            tool_name=decision.tool_name,
            arguments_hash=hashlib.sha256(canonical.encode()).hexdigest(),
            arguments_summary=json.dumps(argument_shape, ensure_ascii=False),
            status="decided",
        )

    async def record_result(
        self,
        context: AgentContext,
        step_index: int,
        result: ToolResult,
    ) -> None:
        run_id = str(context.metadata.get("agent_run_id") or "")
        if not run_id:
            return
        await self.store.record_agent_step(
            run_id=run_id,
            step_index=step_index,
            decision_type="tool_call",
            decision_summary="",
            tool_name="",
            arguments_hash="",
            arguments_summary="",
            result_summary=result.summary,
            status=result.status,
            completed=True,
        )

    async def finish(self, run_id: str, result: AgentResult) -> None:
        await self.store.finish_agent_run(
            run_id,
            status=result.status,
            step_count=result.step_count,
            tool_call_count=result.tool_call_count,
            final_response=result.response,
            error_code=result.error_code,
        )
