from __future__ import annotations

import hashlib
import json
import uuid

from pydantic import BaseModel, ConfigDict, Field

from ..kernel.models import ExecutionContext, IntentExecutionPayload, IntentResult
from ..ports.llm import AgentModelPort
from .catalog import IntentCatalog


class AgentExecuteArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: str = Field(min_length=1, max_length=100_000)


class AgentHandler:
    def __init__(
        self,
        *,
        catalog: IntentCatalog,
        model: AgentModelPort,
        max_steps: int = 8,
    ) -> None:
        self.catalog = catalog
        self.model = model
        self.max_steps = max_steps
        self._dispatcher = None

    def bind_dispatcher(self, dispatcher: object) -> None:
        if self._dispatcher is not None:
            raise RuntimeError("agent dispatcher already bound")
        self._dispatcher = dispatcher

    async def __call__(self, context: ExecutionContext, arguments: BaseModel) -> IntentResult:
        from .dispatcher import IntentDispatcher

        args = AgentExecuteArguments.model_validate(arguments)
        if not isinstance(self._dispatcher, IntentDispatcher):
            raise RuntimeError("agent dispatcher is not bound")
        observations: list[dict] = []
        tools = self.catalog.agent_tools()
        for step in range(self.max_steps):
            decision = await self.model.decide(
                user_text=args.request,
                context=context.contexts,
                observations=tuple(observations),
                tools=tools,
            )
            if decision.kind == "final_answer":
                return IntentResult.success(
                    code="agent.completed",
                    data={"answer": decision.content, "steps": step + 1},
                    message=decision.content,
                    kind="plain",
                )
            if decision.kind == "ask_user":
                return IntentResult(
                    status="clarification_required",
                    code="agent.clarification_required",
                    data={"question": decision.content},
                    message=decision.content,
                )
            try:
                spec = self.catalog.resolve(decision.intent_id)
            except (ValueError, RuntimeError):
                spec = None
            if spec is None or not spec.agent_exposed or spec.intent_id == "agent.execute":
                observations.append(
                    {
                        "status": "failure",
                        "code": "agent.tool_not_allowed",
                        "intent_id": decision.intent_id,
                    }
                )
                continue
            child = context.envelope.model_copy(
                update={
                    "request_id": f"agent:{uuid.uuid4().hex}",
                    "source": "agent",
                    "parent_request_id": context.envelope.request_id,
                    "agent_run_id": context.envelope.agent_run_id or context.execution_id,
                    "metadata": {
                        **context.envelope.metadata,
                        "idempotency_key": hashlib.sha256(
                            json.dumps(
                                {
                                    "agent_run_id": context.envelope.agent_run_id
                                    or context.execution_id,
                                    "step": step,
                                    "intent_id": decision.intent_id,
                                    "arguments": decision.arguments,
                                },
                                sort_keys=True,
                                separators=(",", ":"),
                            ).encode()
                        ).hexdigest(),
                    },
                    "payload": IntentExecutionPayload(
                        intent_id=decision.intent_id,
                        arguments=decision.arguments,
                    ),
                }
            )
            result = await self._dispatcher.execute(child)
            if result.status in {
                "confirmation_required",
                "authorization_required",
                "clarification_required",
            }:
                return result
            observations.append(result.model_dump(mode="json"))
        return IntentResult.failure(
            code="agent.step_limit_reached",
            message="复杂任务达到最大执行步数，请缩小范围后重试。",
            data={"observations": observations},
        )
