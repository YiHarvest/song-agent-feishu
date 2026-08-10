from __future__ import annotations

import httpx
import pytest

from song_agent.app import create_app
from song_agent.bootstrap.v1 import build_runtime
from song_agent.ports.llm import RouteDecision


class FakeIntentModel:
    async def route(self, content, intents, context, *, repair_instruction=""):
        return RouteDecision(
            intent_id="conversation.respond",
            arguments={"text": "hello"},
            confidence=1,
        )


class FakeConversationModel:
    async def respond(self, text, context, *, system_instruction=""):
        return f"answer:{text}"


@pytest.mark.asyncio
async def test_http_has_only_trusted_identity_and_three_execution_surfaces(settings) -> None:
    runtime = build_runtime(settings)
    runtime.router.model = FakeIntentModel()
    runtime.catalog.resolve("conversation.respond").handler.model = FakeConversationModel()
    app = create_app(runtime=runtime)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            health = await client.get("/health")
            assert health.json()["schema_revision"] == "0002_oauth"
            unauthorized = await client.post(
            "/api/v1/intents/plan.get/execute",
            json={"arguments": {"plan_id": "p1"}},
        )
            assert unauthorized.status_code == 401
            headers = {"Authorization": "Bearer test-api-key"}
            forged = await client.post(
                "/api/v1/intents/plan.get/execute",
                headers=headers,
                json={
                    "arguments": {"plan_id": "p1"},
                    "principal": {"principal_id": "admin"},
                },
            )
            assert forged.status_code == 422
            chat = await client.post(
                "/v1/chat/completions",
                headers=headers,
                json={
                    "model": settings.api.model_id,
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )
            assert chat.status_code == 200
            assert chat.json()["choices"][0]["message"]["content"] == "answer:hello"
            paths = set(app.openapi()["paths"])
            assert not any(path.startswith("/api/v1/calendar") for path in paths)
            assert "/api/v1/intents/{intent_id}/execute" in paths
            assert "/api/v1/actions/{action_id}/confirm" in paths


@pytest.mark.asyncio
async def test_openai_streaming_shape(settings) -> None:
    runtime = build_runtime(settings)
    runtime.router.model = FakeIntentModel()
    runtime.catalog.resolve("conversation.respond").handler.model = FakeConversationModel()
    app = create_app(runtime=runtime)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer test-api-key"},
                json={
                    "model": settings.api.model_id,
                    "messages": [{"role": "user", "content": "hello"}],
                    "stream": True,
                },
            )
            assert response.status_code == 200
            assert "chat.completion.chunk" in response.text
            assert response.text.rstrip().endswith("data: [DONE]")
