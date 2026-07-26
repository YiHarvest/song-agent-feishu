"""开放式请求专用 ReAct 入口。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from ..domain.intents import UserRequest
from ..domain.results import ApplicationResult


class GeneralAgent:
    def __init__(
        self,
        handler: Callable[[UserRequest], Awaitable[ApplicationResult]],
    ) -> None:
        self.handler = handler

    async def run(self, request: UserRequest) -> ApplicationResult:
        return await self.handler(request)
