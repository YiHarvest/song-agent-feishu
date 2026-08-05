"""只读端口：Presenter 查询待确认 Action 的边界接口。

由 `modules/pending_actions`（或当前 `PendingActionApplicationService`）
提供实现；Presenter 只依赖本端口，不直接访问 Store。
"""

from __future__ import annotations

from typing import Protocol

from ...models import FeishuIdentity, PendingAction


class PendingActionQueryPort(Protocol):
    async def get_for_presentation(
        self,
        *,
        action_id: str,
        identity: FeishuIdentity,
    ) -> PendingAction | None: ...
