"""飞书 card.action.trigger v2 回调的薄适配器（Q7）。

职责：
- 解析卡片事件 → action_name / action_id / 可信身份
- 显式 action → handler 映射（拒绝未知动作）
- 调用 PendingActionService（confirm / cancel / retry）
- 把 ApplicationResult 转成飞书 toast

不构造 UserRequest、不走 IntentExtractor、不进入 Dispatcher / Agent。
"""

from __future__ import annotations

import asyncio
import logging

from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)

from ...application.pending_action_service import PendingActionApplicationService
from ...models import FeishuIdentity


class FeishuCardHandler:
    def __init__(
        self,
        pending_actions: PendingActionApplicationService,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.pending_actions = pending_actions
        self.loop = loop
        self.logger = logging.getLogger(__name__)
        self.handlers = {
            "pending_action.confirm": pending_actions.confirm,
            "pending_action.cancel": pending_actions.cancel,
            "pending_action.retry": pending_actions.retry,
        }

    def handle(self, callback: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        header = callback.header
        event = callback.event
        value = event.action.value
        action_name = value["action"]
        action_id = value["action_id"]
        operator = event.operator
        identity = FeishuIdentity(
            tenant_key=header.tenant_key,
            app_id=header.app_id,
            open_id=operator.open_id,
            user_id=operator.user_id or "",
            union_id=operator.union_id or "",
        )
        self.logger.info(
            "处理 card.action.trigger event_id=%s action=%s action_id=%s "
            "tenant=%s app=%s actor=%s message_id=%s",
            header.event_id,
            action_name,
            action_id,
            header.tenant_key,
            header.app_id,
            operator.open_id,
            event.context.open_message_id,
        )
        handler = self.handlers.get(action_name)
        if handler is None:
            return P2CardActionTriggerResponse(
                {"toast": {"type": "error", "content": "不支持的卡片操作"}}
            )
        coroutine = handler(identity, action_id, event_id=header.event_id)
        result = asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout=3)
        toast_type = "success" if result.status == "ok" else "error"
        return P2CardActionTriggerResponse(
            {
                "toast": {
                    "type": toast_type,
                    "content": result.message,
                }
            }
        )
