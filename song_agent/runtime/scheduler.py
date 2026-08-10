from __future__ import annotations

from datetime import UTC, datetime

from ..kernel.models import (
    DeliveryTarget,
    IntentExecutionPayload,
    IntentResult,
    PrincipalIdentity,
    RequestEnvelope,
)
from ..ports.repositories import SchedulerLeaseRepository
from .dispatcher import IntentDispatcher


class SchedulerService:
    """Persistent lease/fencing trigger that dispatches a known system intent."""

    def __init__(
        self,
        *,
        owner_id: str,
        leases: SchedulerLeaseRepository,
        dispatcher: IntentDispatcher,
        lease_seconds: int = 30,
    ) -> None:
        self.owner_id = owner_id
        self.leases = leases
        self.dispatcher = dispatcher
        self.lease_seconds = lease_seconds

    async def trigger_broadcast(
        self,
        *,
        job_id: str,
        scheduled_for: datetime,
        requested_by: PrincipalIdentity,
        targets: list[DeliveryTarget],
        message: str,
    ) -> IntentResult | None:
        if not targets:
            raise ValueError("scheduler broadcast requires at least one delivery target")
        claim = await self.leases.claim(
            job_id,
            owner_id=self.owner_id,
            now=datetime.now(UTC),
            lease_seconds=self.lease_seconds,
        )
        if claim is None:
            return None
        request_id = f"scheduler:{job_id}:{scheduled_for.astimezone(UTC).isoformat()}"
        envelope = RequestEnvelope(
            request_id=request_id,
            principal=requested_by,
            delivery_target=targets[0],
            source="scheduler",
            payload=IntentExecutionPayload(
                intent_id="system.scheduler.broadcast",
                arguments={
                    "targets": [target.model_dump(mode="json") for target in targets],
                    "message": message,
                },
            ),
            metadata={
                "idempotency_key": f"{job_id}:{scheduled_for.isoformat()}",
                "fencing_token": claim.fencing_token,
            },
        )
        return await self.dispatcher.execute(envelope)
