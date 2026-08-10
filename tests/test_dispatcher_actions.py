from __future__ import annotations

import uuid

import pytest

from song_agent.bootstrap.v1 import build_runtime
from song_agent.kernel.models import (
    DeliveryTarget,
    IntentExecutionPayload,
    PrincipalIdentity,
    RequestEnvelope,
)
from song_agent.ports.repositories import ActionStatus


def envelope(intent_id: str, arguments: dict, *, request_id: str = "req-1") -> RequestEnvelope:
    principal = PrincipalIdentity(
        tenant_id="default", principal_id="api-client", auth_provider="api_key"
    )
    return RequestEnvelope(
        request_id=request_id,
        principal=principal,
        delivery_target=DeliveryTarget(
            channel="openai_api", channel_account_id="default", destination_id="api-client"
        ),
        source="intent_api",
        payload=IntentExecutionPayload(intent_id=intent_id, arguments=arguments),
        metadata={"idempotency_key": f"idem-{request_id}"},
    )


@pytest.mark.asyncio
async def test_high_risk_action_is_immutable_claimed_and_executed_once(settings) -> None:
    runtime = build_runtime(settings)
    await runtime.start()
    try:
        saved = await runtime.dispatcher.execute(
            envelope("plan.save", {"plan_id": "p1", "plan": {"title": "one"}})
        )
        assert saved.code == "plan.saved"
        pending = await runtime.dispatcher.execute(
            envelope("plan.delete", {"plan_id": "p1"}, request_id="delete-1")
        )
        assert pending.status == "confirmation_required"
        action_id = pending.data["action_id"]
        principal = envelope("plan.get", {}).principal
        action = await runtime.actions.get(action_id, principal)
        assert action is not None
        assert action.status is ActionStatus.PENDING
        assert action.arguments == {"plan_id": "p1"}

        confirmed = await runtime.actions.confirm(action_id, principal)
        assert confirmed.code == "plan.deleted"
        duplicate = await runtime.actions.confirm(action_id, principal)
        assert duplicate.code == "action.not_confirmable"
        final = await runtime.actions.get(action_id, principal)
        assert final is not None and final.status is ActionStatus.SUCCEEDED
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_only_requester_can_confirm(settings) -> None:
    runtime = build_runtime(settings)
    await runtime.start()
    try:
        pending = await runtime.dispatcher.execute(
            envelope("plan.delete", {"plan_id": "p1"}, request_id=str(uuid.uuid4()))
        )
        attacker = PrincipalIdentity(
            tenant_id="default", principal_id="other", auth_provider="api_key"
        )
        result = await runtime.actions.confirm(pending.data["action_id"], attacker)
        assert result.code == "action.not_confirmable"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_write_idempotency_replays_stable_result(settings) -> None:
    runtime = build_runtime(settings)
    await runtime.start()
    try:
        request = envelope("plan.save", {"plan_id": "p1", "plan": {"v": 1}})
        first = await runtime.dispatcher.execute(request)
        second = await runtime.dispatcher.execute(
            request.model_copy(update={"request_id": "different-request-id"})
        )
        assert first == second
    finally:
        await runtime.close()
