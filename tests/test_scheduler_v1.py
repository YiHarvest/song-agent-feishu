from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from song_agent.bootstrap.v1 import build_runtime
from song_agent.infrastructure.database import Database
from song_agent.infrastructure.repositories import SqliteSchedulerLeaseRepository
from song_agent.kernel.models import DeliveryTarget, PrincipalIdentity


@pytest.mark.asyncio
async def test_scheduler_lease_uses_persistent_fencing_tokens(database_path) -> None:
    repository = SqliteSchedulerLeaseRepository(Database(database_path))
    now = datetime.now(UTC)
    first = await repository.claim("job", owner_id="one", now=now, lease_seconds=30)
    blocked = await repository.claim("job", owner_id="two", now=now, lease_seconds=30)
    takeover = await repository.claim(
        "job", owner_id="two", now=now + timedelta(seconds=31), lease_seconds=30
    )
    assert first is not None and first.fencing_token == 1
    assert blocked is None
    assert takeover is not None and takeover.fencing_token == 2


@pytest.mark.asyncio
async def test_scheduler_dispatches_known_system_intent_without_router(settings) -> None:
    runtime = build_runtime(settings)
    await runtime.start()
    try:
        result = await runtime.scheduler.trigger_broadcast(
            job_id="morning",
            scheduled_for=datetime(2026, 8, 7, 8, tzinfo=UTC),
            requested_by=PrincipalIdentity(
                tenant_id="default",
                principal_id="api-client",
                auth_provider="api_key",
            ),
            targets=[
                DeliveryTarget(
                    channel="openai_api",
                    channel_account_id="default",
                    destination_id="api-client",
                )
            ],
            message="morning",
        )
        assert result is not None
        assert result.status == "confirmation_required"
        assert result.data["intent_id"] == "system.scheduler.broadcast"
    finally:
        await runtime.close()
