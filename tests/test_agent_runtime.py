from pathlib import Path
from typing import Any

import pytest

from song_agent.agent.context import AgentContext
from song_agent.agent.context_builder import AgentContextBuilder
from song_agent.agent.models import AgentDecision, ToolResult
from song_agent.agent.runtime import AgentLimits, ReActRuntime
from song_agent.agent.tool_registry import AgentTool, ToolRegistry
from song_agent.config import Settings
from song_agent.llm import LLMOutputTruncatedError
from song_agent.models import IncomingMessage
from song_agent.policies.tool_policy import ToolPolicyGuard
from song_agent.services.agent_runs import AgentRunRecorder
from song_agent.services.encryption import AesGcmTokenCipher
from song_agent.store import SqliteStore
from song_agent.workflow import AgentWorkflow


class FakeLlm:
    def __init__(self, decisions: list[AgentDecision]) -> None:
        self.decisions = iter(decisions)

    async def generate(self, schema, system: str, user: str, **kwargs):
        del schema, system, user, kwargs
        return next(self.decisions)


def context() -> AgentContext:
    message = IncomingMessage(
        message_id="message",
        app_id="app",
        user_id="user",
        open_id="open",
        chat_id="chat",
        chat_type="p2p",
        message_type="text",
        text="创建计划",
    )
    return AgentContext(message, message.text, "conversation")


def runtime(
    decisions: list[AgentDecision],
    handler,
) -> ReActRuntime:
    registry = ToolRegistry()
    registry.register(
        AgentTool(
            name="plans.get_today",
            description="read plan",
            handler=handler,
            category="read",
        )
    )
    return ReActRuntime(
        FakeLlm(decisions),  # type: ignore[arg-type]
        registry,
        ToolPolicyGuard(),
        AgentLimits(max_steps=4, max_tool_calls=3, timeout_seconds=5),
    )


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "feishu_app_id": "app",
        "feishu_app_secret": "secret",
        "llm_base_url": "https://llm.example/v1",
        "llm_api_key": "key",
        "llm_model": "model",
        "agent_run_timeout_seconds": 60,
        "agent_step_timeout_seconds": 20,
        "agent_finish_reserve_seconds": 5,
    }
    values.update(overrides)
    return Settings(**values)


def test_settings_accepts_configured_fifteen_tool_calls() -> None:
    assert settings(agent_max_tool_calls=15).agent_max_tool_calls == 15


def test_context_builder_preserves_runtime_tool_summary() -> None:
    summary = AgentContextBuilder().build_tool_result_summary(
        [
            {
                "tool": "plans.save_draft",
                "status": "ok",
                "summary": "计划草稿已保存",
            }
        ]
    )

    assert "成功：计划草稿已保存" in summary
    assert "plans.save_draft: 完成" not in summary


def test_context_builder_preserves_nested_plan_tool_schema() -> None:
    workflow = object.__new__(AgentWorkflow)
    plan_tool = workflow._build_tool_registry().get("plans.save_draft")
    assert plan_tool is not None

    prompt = AgentContextBuilder().build_system_prompt(
        context(),
        tool_schemas=[plan_tool.schema()],
    )

    assert '"required":["title","priority","start_time","end_time","repeat"]' in prompt
    assert '"title":{"type":"string"' in prompt
    assert "名称字段必须是 title，不是 name" in prompt
    assert "不要使用 ISO" in prompt


@pytest.mark.asyncio
async def test_runtime_uses_final_limit_and_compacts_after_truncation() -> None:
    class TruncatingLlm:
        def __init__(self) -> None:
            self.calls = []

        async def generate(self, schema, system, user, **kwargs):
            del schema, user
            self.calls.append((system, kwargs["max_tokens"]))
            if len(self.calls) == 1:
                raise LLMOutputTruncatedError("length")
            return AgentDecision(type="final_answer", content="完整摘要")

    llm = TruncatingLlm()
    configured = settings()
    agent = ReActRuntime(
        llm,
        ToolRegistry(),
        ToolPolicyGuard(),
        AgentLimits(),
        settings=configured,
    )
    ctx = context()
    ctx.user_text = "总结今天的 AI 新闻"

    result = await agent.run(ctx)

    assert result.status == "completed"
    assert result.response == "完整摘要"
    assert [max_tokens for _, max_tokens in llm.calls] == [
        configured.llm_final_max_tokens,
        configured.llm_final_max_tokens,
    ]
    assert "减少条目数量" in llm.calls[1][0]


def test_agent_decision_accepts_long_complete_answer() -> None:
    decision = AgentDecision(type="final_answer", content="完整内容" * 2000)

    assert len(decision.content) == 8000


@pytest.mark.asyncio
async def test_runtime_feeds_tool_observation_back_then_finishes() -> None:
    calls = 0

    async def handler(ctx: AgentContext, arguments: dict[str, Any]) -> ToolResult:
        nonlocal calls
        del ctx, arguments
        calls += 1
        return ToolResult(status="ok", summary="今天有两项计划")

    agent = runtime(
        [
            AgentDecision(
                type="tool_call",
                tool_name="plans.get_today",
                decision_summary="查询计划",
            ),
            AgentDecision(type="final_answer", content="今天有两项计划"),
        ],
        handler,
    )
    result = await agent.run(context())
    assert result.status == "completed"
    assert result.step_count == 2
    assert result.tool_call_count == 1
    assert calls == 1


@pytest.mark.asyncio
async def test_budget_allows_exact_configured_number_of_tool_calls() -> None:
    calls = 0

    async def handler(ctx: AgentContext, arguments: dict[str, Any]) -> ToolResult:
        nonlocal calls
        del ctx, arguments
        calls += 1
        return ToolResult(status="ok", summary=f"完成第 {calls} 项")

    registry = ToolRegistry()
    registry.register(
        AgentTool(
            name="plans.get_today",
            description="read plan",
            handler=handler,
            category="read",
        )
    )
    agent = ReActRuntime(
        FakeLlm(
            [
                AgentDecision(
                    type="tool_call",
                    tool_name="plans.get_today",
                    arguments={"item": 1},
                ),
                AgentDecision(
                    type="tool_call",
                    tool_name="plans.get_today",
                    arguments={"item": 2},
                ),
                AgentDecision(type="final_answer", content="两项均已完成"),
            ]
        ),
        registry,
        ToolPolicyGuard(),
        AgentLimits(max_steps=3, max_tool_calls=2, timeout_seconds=60),
        settings=settings(
            agent_max_steps=3,
            agent_max_llm_requests=3,
            agent_max_tool_calls=2,
        ),
    )

    result = await agent.run(context())

    assert result.status == "completed"
    assert result.tool_call_count == 2
    assert calls == 2


@pytest.mark.asyncio
async def test_runtime_rejects_repeated_identical_tool_call() -> None:
    async def handler(ctx: AgentContext, arguments: dict[str, Any]) -> ToolResult:
        del ctx, arguments
        return ToolResult(status="ok", summary="same")

    decision = AgentDecision(type="tool_call", tool_name="plans.get_today")
    result = await runtime([decision, decision], handler).run(context())
    assert result.status == "failed"
    assert result.error_code == "repeated_tool_call"


@pytest.mark.asyncio
async def test_weather_alias_uses_visible_websearch_tool() -> None:
    calls = []

    async def handler(ctx: AgentContext, arguments: dict[str, Any]) -> ToolResult:
        calls.append(arguments)
        return ToolResult(status="ok", summary="杭州萧山晴，30℃")

    registry = ToolRegistry()
    registry.register(
        AgentTool(
            name="websearch.search",
            description="search web",
            handler=handler,
            category="local",
            arguments_schema={
                "type": "object",
                "required": ["query"],
                "properties": {"query": {"type": "string"}},
            },
        )
    )
    agent = ReActRuntime(
        FakeLlm(
            [
                AgentDecision(
                    type="tool_call",
                    tool_name="get_weather",
                    arguments={"location": "杭州萧山"},
                ),
                AgentDecision(type="final_answer", content="杭州萧山晴，30℃"),
            ]
        ),
        registry,
        ToolPolicyGuard(),
        AgentLimits(max_steps=3, max_tool_calls=2, timeout_seconds=5),
    )
    ctx = context()
    ctx.user_text = "今天杭州萧山天气怎么样？"

    result = await agent.run(ctx)

    assert result.status == "completed"
    assert calls == [
        {
            "query": "今天杭州萧山天气怎么样？",
            "provider": "auto",
            "max_results": 5,
        }
    ]


@pytest.mark.asyncio
async def test_latest_news_forces_websearch_when_model_claims_tool_missing() -> None:
    calls = []

    async def handler(ctx: AgentContext, arguments: dict[str, Any]) -> ToolResult:
        calls.append(arguments)
        return ToolResult(status="ok", summary="找到最新消息")

    registry = ToolRegistry()
    registry.register(
        AgentTool(
            name="websearch.search",
            description="search web",
            handler=handler,
            category="local",
        )
    )
    agent = ReActRuntime(
        FakeLlm(
            [
                AgentDecision(
                    type="final_answer",
                    content="没有可用网络搜索工具",
                ),
                AgentDecision(type="final_answer", content="最新消息摘要"),
            ]
        ),
        registry,
        ToolPolicyGuard(),
        AgentLimits(max_steps=3, max_tool_calls=2, timeout_seconds=5),
    )
    ctx = context()
    ctx.user_text = "谷歌GPT最新消息"

    result = await agent.run(ctx)

    assert result.status == "completed"
    assert calls == [
        {
            "query": "谷歌GPT最新消息",
            "provider": "auto",
            "max_results": 5,
        }
    ]
    assert result.response == "最新消息摘要"


def test_registry_rejects_llm_visible_commit_tools() -> None:
    async def handler(ctx: AgentContext, arguments: dict[str, Any]) -> ToolResult:
        del ctx, arguments
        return ToolResult(status="ok", summary="never")

    registry = ToolRegistry()
    with pytest.raises(ValueError):
        registry.register(
            AgentTool(
                name="calendar.commit_create_event",
                description="must stay internal",
                handler=handler,
                category="prepare",
            )
        )


def test_workflow_exposes_single_pass_structured_tools_and_no_chat_tool() -> None:
    workflow = object.__new__(AgentWorkflow)
    registry = workflow._build_tool_registry()

    assert registry.get("chat.reply") is None
    plan = registry.get("plans.save_draft")
    document = registry.get("documents.prepare_create")
    assert plan is not None
    assert plan.arguments_schema["required"] == ["tasks"]
    assert document is not None
    assert document.arguments_schema["required"] == ["title", "target_title", "markdown"]


def test_planning_phrase_selects_plan_tool_schema() -> None:
    workflow = object.__new__(AgentWorkflow)
    registry = workflow._build_tool_registry()
    agent = ReActRuntime(
        FakeLlm([]),
        registry,
        ToolPolicyGuard(),
        AgentLimits(),
    )
    ctx = context()
    ctx.user_text = "我今天要吃饭、睡觉、购物，提交宋管家的PR，给我规划一下"

    capabilities = agent._infer_capabilities(ctx, [])
    schemas = registry.schemas_for(ctx, capabilities)

    assert "plans" in capabilities
    assert {schema["name"] for schema in schemas} == {
        "plans.save_draft",
        "reviews.save",
    }


@pytest.mark.parametrize(
    "phrase",
    [
        "10分钟后提醒我交PR",
        "帮我设个闹钟",
        "明天上午9点定时提醒我开会",
        "到点提醒我喝水",
        "remind me to submit the PR in 10 minutes",
    ],
)
def test_reminder_phrase_does_not_expose_calendar_react_tool(phrase: str) -> None:
    """确定性日历意图不能进入 ReAct 工具路径。"""
    workflow = object.__new__(AgentWorkflow)
    registry = workflow._build_tool_registry()
    agent = ReActRuntime(
        FakeLlm([]),
        registry,
        ToolPolicyGuard(),
        AgentLimits(),
    )
    ctx = context()
    ctx.user_text = phrase

    capabilities = agent._infer_capabilities(ctx, [])
    schemas = registry.schemas_for(ctx, capabilities)

    assert "calendar" not in capabilities
    schema_names = {schema["name"] for schema in schemas}
    assert "calendar.prepare_create_event" not in schema_names


@pytest.mark.asyncio
async def test_agent_history_keeps_summaries_and_hashes_without_raw_arguments(
    tmp_path: Path,
) -> None:
    store = SqliteStore(
        tmp_path / "state.db",
        app_id="app",
        token_cipher=AesGcmTokenCipher({1: b"1" * 32}, 1),
    )
    await store.initialize()
    try:

        async def handler(
            ctx: AgentContext,
            arguments: dict[str, Any],
        ) -> ToolResult:
            del ctx, arguments
            return ToolResult(status="ok", summary="read complete")

        registry = ToolRegistry()
        registry.register(
            AgentTool(
                name="plans.get_today",
                description="read plan",
                handler=handler,
                category="read",
            )
        )
        recorder = AgentRunRecorder(store, "test-model")
        ctx = context()
        run_id = await recorder.start(ctx)
        agent = ReActRuntime(
            FakeLlm(
                [
                    AgentDecision(
                        type="tool_call",
                        tool_name="plans.get_today",
                        arguments={"token": "must-not-be-stored", "count": 1},
                        decision_summary="读取计划摘要",
                    ),
                    AgentDecision(type="final_answer", content="done"),
                ]
            ),  # type: ignore[arg-type]
            registry,
            ToolPolicyGuard(),
            AgentLimits(max_steps=4, max_tool_calls=3, timeout_seconds=5),
            recorder=recorder,
        )
        result = await agent.run(ctx)
        await recorder.finish(run_id, result)
        run = await (
            await store.db.execute("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,))
        ).fetchone()
        steps = await (
            await store.db.execute(
                "SELECT * FROM agent_steps WHERE run_id = ? ORDER BY step_index",
                (run_id,),
            )
        ).fetchall()
        serialized = repr([dict(row) for row in steps])
        assert run["status"] == "completed"
        assert run["step_count"] == 2
        assert len(steps) == 2
        assert steps[0]["arguments_hash"]
        assert '"token": "str"' in steps[0]["arguments_summary"]
        assert "must-not-be-stored" not in serialized
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_stale_agent_run_is_recovered_after_process_interruption(
    tmp_path: Path,
) -> None:
    store = SqliteStore(
        tmp_path / "state.db",
        app_id="app",
        token_cipher=AesGcmTokenCipher({1: b"1" * 32}, 1),
    )
    await store.initialize()
    try:
        recorder = AgentRunRecorder(store, "test-model")
        run_id = await recorder.start(context())
        await store.db.execute(
            "UPDATE agent_runs SET started_at = 0 WHERE run_id = ?",
            (run_id,),
        )
        assert await store.recover_stale_agent_runs(max_age_seconds=120) == 1
        row = await (
            await store.db.execute(
                "SELECT status, error_code FROM agent_runs WHERE run_id = ?",
                (run_id,),
            )
        ).fetchone()
        assert row["status"] == "interrupted"
        assert row["error_code"] == "process_interrupted"
    finally:
        await store.close()
