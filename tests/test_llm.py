import logging
import time
from types import SimpleNamespace

import httpx
import pytest

from song_agent.agent.models import AgentDecision
from song_agent.llm import (
    LLMOutputTruncatedError,
    StructuredLlm,
    api_error_message,
)


def test_api_error_message_supports_openai_shape() -> None:
    response = httpx.Response(
        400,
        json={"error": {"message": "invalid model"}},
    )

    assert api_error_message(response) == "invalid model"


def test_api_error_message_supports_siliconflow_shape() -> None:
    response = httpx.Response(
        400,
        json={"code": 20012, "message": "Model does not exist", "data": None},
    )

    assert api_error_message(response) == "Model does not exist (code=20012)"


def test_api_error_message_ignores_unstructured_body() -> None:
    response = httpx.Response(502, text="<html>bad gateway</html>")

    assert api_error_message(response) is None


@pytest.mark.asyncio
async def test_structured_llm_rejects_length_finished_json() -> None:
    class Completions:
        async def create(self, **kwargs):
            del kwargs
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        finish_reason="length",
                        message=SimpleNamespace(
                            content='{"type":"final_answer","content":"半截",'
                            '"tool_name":"","arguments":{},"decision_summary":""}'
                        ),
                    )
                ]
            )

    llm = object.__new__(StructuredLlm)
    llm.client = SimpleNamespace(
        chat=SimpleNamespace(completions=Completions())
    )
    llm.settings = SimpleNamespace(llm_model="test-model")
    llm.logger = logging.getLogger("test.llm")
    llm._retry_counts = {"run:0": 0}

    with pytest.raises(LLMOutputTruncatedError):
        await llm._generate_with_retry(
            schema=AgentDecision,
            system="system",
            user="user",
            max_tokens=100,
            run_id="run",
            step_index=0,
            request_fingerprint="fingerprint",
            tool_schema_count=0,
            started_at=time.perf_counter(),
        )
