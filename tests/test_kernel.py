from __future__ import annotations

import pytest
from pydantic import ValidationError

from song_agent.kernel.ids import validate_intent_id
from song_agent.kernel.models import (
    DeliveryTarget,
    IntentExecutionPayload,
    NaturalLanguagePayload,
    PrincipalIdentity,
    RequestEnvelope,
    TextPart,
)


def test_open_intent_id_validation() -> None:
    assert validate_intent_id("knowledge.search") == "knowledge.search"
    assert validate_intent_id("calendar.event.create") == "calendar.event.create"
    for invalid in ("search", "Calendar.create", "calendar-create", "calendar..create"):
        with pytest.raises(ValueError):
            validate_intent_id(invalid)


def test_request_payloads_are_discriminated_and_mutually_exclusive() -> None:
    common = {
        "request_id": "req-1",
        "principal": PrincipalIdentity(
            tenant_id="tenant", principal_id="user", auth_provider="test"
        ),
        "delivery_target": DeliveryTarget(
            channel="test", channel_account_id="app", destination_id="user"
        ),
        "source": "intent_api",
    }
    natural = RequestEnvelope(
        **common,
        payload=NaturalLanguagePayload(content=(TextPart(text="hello"),)),
    )
    known = RequestEnvelope(
        **common,
        payload=IntentExecutionPayload(intent_id="plan.get", arguments={"plan_id": "p1"}),
    )
    assert natural.payload.kind == "natural_language"
    assert known.payload.kind == "intent_execution"
    with pytest.raises(ValidationError):
        RequestEnvelope.model_validate(
            {
                **common,
                "payload": {
                    "kind": "natural_language",
                    "content": [{"kind": "text", "text": "hello"}],
                    "intent_id": "plan.get",
                },
            }
        )


def test_principal_and_delivery_target_are_frozen() -> None:
    principal = PrincipalIdentity(
        tenant_id="tenant", principal_id="user", auth_provider="test"
    )
    with pytest.raises(ValidationError):
        principal.principal_id = "attacker"
