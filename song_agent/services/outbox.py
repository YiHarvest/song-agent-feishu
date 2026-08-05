"""已确认外部操作的持久化发件箱消费者。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from ..domain.results import ExecutionContext, ExecutionResult
from ..executors.registry import ExecutorNotFound
from ..models import PendingAction
from ..store import SqliteStore

ActionHandler = Callable[[PendingAction, ExecutionContext], Awaitable[ExecutionResult]]
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
        action_ids = await self.store.ready_outbox_action_ids()
        if action_ids:
            self.logger.info(
                "Action outbox 发现待执行动作 count=%d action_ids=%s",
                len(action_ids),
                action_ids,
            )
        for action_id in action_ids:
            action = await self.store.get_pending_action(action_id)
            if action is not None:
                self.logger.info(
                    "Action outbox 开始分发 action_id=%s action_type=%s status=%s",
                    action.action_id,
                    action.action_type,
                    action.status,
                )
                try:
                    await self.execute(
                        action,
                        ExecutionContext(worker_id=f"outbox:{action.action_id}"),
                    )
                except ExecutorNotFound:
                    # 未注册执行器的动作是结构性问题，不能进入普通重试分支。
                    await self.store.complete_pending_action(
                        action.action_id,
                        status="failed_final",
                        error_code="executor_not_found",
                        error_message=f"no executor for action type: {action.action_type}",
                    )
                    self.logger.error(
                        "Action outbox 无执行器 action_id=%s action_type=%s，已终止",
                        action.action_id,
                        action.action_type,
                    )
                except Exception:
                    self.logger.exception(
                        "Action outbox 分发异常 action_id=%s action_type=%s",
                        action.action_id,
                        action.action_type,
                    )
                    raise
                latest = await self.store.get_pending_action(action_id)
                self.logger.info(
                    "Action outbox 分发完成 action_id=%s status=%s attempt_count=%s",
                    action_id,
                    latest.status if latest else "missing",
                    latest.attempt_count if latest else "unknown",
                )
            else:
                self.logger.error(
                    "Action outbox 引用了不存在的动作 action_id=%s",
                    action_id,
                )
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
