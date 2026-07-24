"""基于 SQLite 的调度器领导租约。"""

from __future__ import annotations

from ..store import SqliteStore


class SchedulerLease:
    def __init__(
        self,
        store: SqliteStore,
        *,
        lease_name: str,
        holder_id: str,
        lease_seconds: int = 60,
    ) -> None:
        self.store = store
        self.lease_name = lease_name
        self.holder_id = holder_id
        self.lease_seconds = lease_seconds

    async def acquire(self) -> int | None:
        return await self.store.acquire_scheduler_lease(
            self.lease_name,
            self.holder_id,
            lease_seconds=self.lease_seconds,
        )

    async def is_current(self, fencing_token: int) -> bool:
        return await self.store.validate_scheduler_lease(
            self.lease_name,
            self.holder_id,
            fencing_token,
        )
