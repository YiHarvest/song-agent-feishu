"""按业务 action_type 分发执行器。"""

from __future__ import annotations

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


class ExecutorNotFound(RuntimeError):
    """未注册执行器的 Action Type。

    Outbox 捕获本异常后必须分类为不可重试终态，不能进入普通重试分支。
    """

    def __init__(self, action_type: str) -> None:
        super().__init__(f"no executor for action type: {action_type}")
        self.action_type = action_type


class ExecutorRegistry:
    def __init__(self) -> None:
        self._executors: dict[str, ActionExecutor] = {}

    def register(self, executor: ActionExecutor) -> None:
        if executor.action_type in self._executors:
            raise ValueError(f"duplicate executor: {executor.action_type}")
        self._executors[executor.action_type] = executor

    async def execute(
        self,
        action: PendingAction,
        context: ExecutionContext,
    ) -> ExecutionResult:
        executor = self._executors.get(action.action_type)
        if executor is None:
            raise ExecutorNotFound(action.action_type)
        return await executor.execute(action, context)
