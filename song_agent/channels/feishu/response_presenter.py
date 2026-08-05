"""输出编排：把 ApplicationResult 渲染并发送到飞书。

职责（Q9）：
- 普通结果 → Renderer.render_result → Transport.send_markdown
- 待确认结果 → PendingActionQueryPort 查询 Action → Renderer.render_confirmation
  → Transport.send_card
- 只读端口查询必须校验 Action 归属；Action 缺失时不发送虚假成功信息。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from ...domain.results import ApplicationResult
from ...models import FeishuIdentity
from .ports import PendingActionQueryPort
from .renderer import FeishuRenderer

if TYPE_CHECKING:
    from ...feishu.transport import FeishuTransport


class MissingConfirmationAction(RuntimeError):
    """awaiting_confirmation 结果缺少 action id。"""


class ConfirmationActionNotFound(RuntimeError):
    """待确认结果引用了不存在的 Action。"""


CardMessageBinder = Callable[[str, str], Awaitable[None]]


def confirmation_action_ids(result: ApplicationResult) -> list[str]:
    """兼容 `action_id` 与 `data["action_ids"]` 两种来源。"""
    value = result.data.get("action_ids")
    if isinstance(value, list):
        return [str(item) for item in value if item]
    if result.action_id:
        return [result.action_id]
    return []


class FeishuResponsePresenter:
    def __init__(
        self,
        pending_actions: PendingActionQueryPort,
        renderer: FeishuRenderer,
        transport: FeishuTransport,
        *,
        card_message_binder: CardMessageBinder | None = None,
    ) -> None:
        self.pending_actions = pending_actions
        self.renderer = renderer
        self.transport = transport
        self.card_message_binder = card_message_binder
        self.logger = logging.getLogger(__name__)

    async def present(
        self,
        chat_id: str,
        identity: FeishuIdentity,
        result: ApplicationResult,
    ) -> None:
        if result.status == "awaiting_confirmation":
            await self._present_confirmations(chat_id, identity, result)
            return
        if result.message:
            markdown = self.renderer.render_result(result)
            await self.transport.send_markdown(chat_id, markdown)

    async def present_resumed(
        self,
        chat_id: str,
        identity: FeishuIdentity,
        result: ApplicationResult,
    ) -> None:
        """OAuth 恢复后的输出（与 present 相同路径）。"""
        await self.present(chat_id, identity, result)

    async def _present_confirmations(
        self,
        chat_id: str,
        identity: FeishuIdentity,
        result: ApplicationResult,
    ) -> None:
        action_ids = confirmation_action_ids(result)
        if not action_ids:
            raise MissingConfirmationAction(
                "awaiting_confirmation result has no action id"
            )
        for action_id in action_ids:
            action = await self.pending_actions.get_for_presentation(
                action_id=action_id,
                identity=identity,
            )
            if action is None:
                # 不发送"已创建"之类误导文本，也不泄露 Action ID。
                self.logger.error(
                    "确认卡片引用了不存在的 Action chat=%s principal=%s",
                    chat_id,
                    identity.subject_id,
                )
                await self.transport.send_markdown(
                    chat_id,
                    "该操作已失效，请重新发起请求。",
                )
                continue
            card = self.renderer.render_confirmation(action)
            message_id = await self.transport.send_card(chat_id, card)
            if message_id and self.card_message_binder is not None:
                await self.card_message_binder(action.action_id, message_id)
