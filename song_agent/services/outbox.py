"""已确认外部操作的持久化发件箱消费者。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from ..models import PendingAction
from ..store import SqliteStore

ActionHandler = Callable[[PendingAction], Awaitable[None]]
ReconcileHandler = Callable[[PendingAction], Awaitable[bool]]


class ActionOutboxWorker:
    def __init__(
        self,
        store: SqliteStore,
        execute: ActionHandler,
        reconcile: ReconcileHandler,
        *,
        poll_seconds: float = 1.0,
    ) -> None:
        self.store = store
        self.execute = execute
        self.reconcile = reconcile
        self.poll_seconds = poll_seconds
        self.logger = logging.getLogger(__name__)
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="action-outbox")

    def notify(self) -> None:
        self._wake.set()

    async def close(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    async def run_once(self) -> None:
        recovered = await self.store.recover_expired_action_claims()
        if recovered:
            self.logger.warning(
                "检测到 %d 个执行租约过期的动作，已转入远端状态核对",
                recovered,
            )
        for action_id in await self.store.ready_outbox_action_ids():
            action = await self.store.get_pending_action(action_id)
            if action is not None:
                await self.execute(action)
        for action in await self.store.unknown_remote_actions():
            await self.reconcile(action)

    async def _run(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("Action outbox 消费失败")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                pass
            self._wake.clear()
