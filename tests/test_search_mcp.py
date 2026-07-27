"""Test search MCP integration."""

from types import SimpleNamespace

import pytest

from song_agent.agent.runtime import ReActRuntime
from song_agent.config import Settings
from song_agent.search.mcp import SearchMcp, SearchResult
from song_agent.workflow import AgentWorkflow, _format_stored_tool_result


@pytest.mark.asyncio
async def test_search_mcp_initialization():
    """Test SearchMcp can be initialized."""
    settings = Settings(
        feishu_app_id="test_app",
        feishu_app_secret="test_secret",
        llm_base_url="https://api.example.com/v1",
        llm_api_key="test_key",
        llm_model="test-model",
        ydc_api_key="test_ydc_key",
        tavily_api_key="test_tavily_key",
    )

    search_mcp = SearchMcp(settings)
    assert search_mcp.settings.ydc_api_key == "test_ydc_key"
    assert search_mcp.settings.tavily_api_key == "test_tavily_key"


def test_search_result_dataclass():
    """Test SearchResult dataclass."""
    result = SearchResult(
        title="Test Title",
        url="https://example.com",
        snippet="Test snippet",
        source="you",
    )

    assert result.title == "Test Title"
    assert result.url == "https://example.com"
    assert result.snippet == "Test snippet"
    assert result.source == "you"


def test_open_agent_exposes_search_mcp_for_search_requests() -> None:
    workflow = object.__new__(AgentWorkflow)
    registry = workflow._build_tool_registry()
    runtime = object.__new__(ReActRuntime)
    context = SimpleNamespace(user_text="检索 Microsoft Agent Framework Harness")

    capabilities = runtime._infer_capabilities(context, [])
    schemas = registry.schemas_for(context, capabilities)

    assert capabilities == {"websearch"}
    assert {schema["name"] for schema in schemas} == {
        "websearch.search",
        "tool_results.read",
    }


@pytest.mark.asyncio
async def test_search_result_returns_to_agent_as_observation() -> None:
    class FakeSearchMcp:
        async def search(self, query, **kwargs):
            assert query == "Harness"
            return [
                SearchResult(
                    title="Agent Harnesses",
                    url="https://learn.microsoft.com/example",
                    snippet="Context providers and compaction.",
                    source="searxng",
                )
            ]

    class Store:
        async def save_tool_result(self, **kwargs):
            return "tool_result_1"

    workflow = object.__new__(AgentWorkflow)
    workflow.search_mcp = FakeSearchMcp()
    workflow.store = Store()
    metadata = {
        "retrieved_context": [
            {
                "source_type": "audio",
                "attachment_id": "att_" + "a" * 32,
                "transcript": "检索 Harness",
            }
        ]
    }
    result = await workflow._tool_websearch(
        SimpleNamespace(
            message=SimpleNamespace(
                tenant_key="tenant",
                app_id="app",
                user_id="user",
            ),
            metadata=metadata,
        ),
        {"query": "Harness", "max_results": 3},
    )

    assert result.status == "ok"
    assert result.terminal is False
    assert "Agent Harnesses" in result.summary
    assert "https://learn.microsoft.com/example" in result.summary
    assert metadata["retrieved_context"][1]["result_ref"] == "tool_result_1"
    assert metadata["retrieved_context"][1]["items"][0]["title"] == "Agent Harnesses"


@pytest.mark.asyncio
async def test_tool_result_read_appends_to_attachment_context_list() -> None:
    class Store:
        async def get_tool_result(self, *args, **kwargs):
            return {
                "summary": "找到 1 条结果",
                "payload": {
                    "items": [
                        {
                            "title": "天气",
                            "snippet": "杭州高温。",
                            "url": "https://example.com/weather",
                            "source": "test",
                        }
                    ]
                },
            }

    workflow = object.__new__(AgentWorkflow)
    workflow.store = Store()
    metadata = {"retrieved_context": [{"source_type": "audio"}]}
    result = await workflow._tool_result_read(
        SimpleNamespace(
            message=SimpleNamespace(
                tenant_key="tenant",
                app_id="app",
                user_id="user",
            ),
            metadata=metadata,
        ),
        {"result_ref": "tool_result_1"},
    )

    assert result.status == "ok"
    assert metadata["retrieved_context"][1]["result_ref"] == "tool_result_1"
    assert "杭州高温" in metadata["retrieved_context"][1]["content"]


def test_stored_search_result_compacts_at_source_boundaries() -> None:
    formatted = _format_stored_tool_result(
        {
            "summary": "找到 4 条结果",
            "payload": {
                "items": [
                    {
                        "title": f"新闻 {index}",
                        "source": f"来源 {index}",
                        "snippet": "完整句子。" * 80,
                        "url": f"https://example.com/{index}",
                    }
                    for index in range(4)
                ]
            },
        },
        max_length=700,
    )

    assert len(formatted) <= 700
    assert "[来源 0] 新闻 0" in formatted
    assert "其余" in formatted
    assert not formatted.endswith("完整句")
