from __future__ import annotations

import pytest
from test_dispatcher_actions import envelope

from song_agent.bootstrap.v1 import build_runtime
from song_agent.ports.llm import AgentDecision


class FakeAgentModel:
    def __init__(self) -> None:
        self.calls = 0
        self.tools: set[str] = set()
        self.observations: tuple[dict, ...] = ()

    async def decide(self, *, user_text, context, observations, tools):
        self.calls += 1
        self.tools = {tool.intent_id for tool in tools}
        self.observations = observations
        if self.calls == 1:
            return AgentDecision(
                kind="intent_call",
                intent_id="plan.get",
                arguments={"plan_id": "p1"},
            )
        return AgentDecision(kind="final_answer", content="done")


@pytest.mark.asyncio
async def test_agent_uses_catalog_tools_and_dispatches_known_intent_without_router(settings) -> None:
    runtime = build_runtime(settings)
    await runtime.start()
    try:
        await runtime.dispatcher.execute(
            envelope("plan.save", {"plan_id": "p1", "plan": {"title": "one"}})
        )
        model = FakeAgentModel()
        runtime.catalog.resolve("agent.execute").handler.model = model
        result = await runtime.dispatcher.execute(
            envelope("agent.execute", {"request": "load my plan"}, request_id="agent-1")
        )
        assert result.code == "agent.completed"
        assert result.data["answer"] == "done"
        assert "plan.get" in model.tools
        assert "agent.execute" not in model.tools
        assert model.observations[0]["code"] == "plan.loaded"
        assert model.observations[0]["data"]["plan"]["title"] == "one"
    finally:
        await runtime.close()
