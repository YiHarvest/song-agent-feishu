"""按业务 action_type 分发执行器。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from ..domain.results import ExecutionContext, ExecutionResult
from ..models import PendingAction


class ActionExecutor(Protocol):
    action_type: str

    async def execute(
        self,
        pending_action: PendingAction,
        context: ExecutionContext,
    ) -> ExecutionResult: ...


class ExecutorRegistry:
    def __init__(
        self,
        *,
        legacy_handler: Callable[[PendingAction], Awaitable[None]] | None = None,
    ) -> None:
        self.executors: dict[str, ActionExecutor] = {}
        self.legacy_handler = legacy_handler

    def register(self, executor: ActionExecutor) -> None:
        if executor.action_type in self.executors:
            raise ValueError(f"duplicate executor: {executor.action_type}")
        self.executors[executor.action_type] = executor

    async def execute(self, action: PendingAction) -> None:
        executor = self.executors.get(action.action_type)
        if executor:
            await executor.execute(
                action,
                ExecutionContext(worker_id=f"executor:{action.action_type}"),
            )
            return
        if self.legacy_handler:
            await self.legacy_handler(action)
            return
        raise ValueError(f"no executor for action type: {action.action_type}")
