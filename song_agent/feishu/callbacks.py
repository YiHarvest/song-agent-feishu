"""飞书 card.action.trigger v2 回调适配。"""

from __future__ import annotations

import asyncio
import logging

from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)

from ..application.pending_action_service import PendingActionApplicationService
from ..models import FeishuIdentity


class FeishuCardCallbacks:
    def __init__(
        self,
        service: PendingActionApplicationService,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.service = service
        self.loop = loop
        self.logger = logging.getLogger(__name__)

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
        if action_name == "pending_action.confirm":
            coroutine = self.service.confirm(identity, action_id, event_id=header.event_id)
        elif action_name == "pending_action.cancel":
            coroutine = self.service.cancel(identity, action_id, event_id=header.event_id)
        else:
            return P2CardActionTriggerResponse(
                {"toast": {"type": "error", "content": "不支持的卡片操作"}}
            )
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
