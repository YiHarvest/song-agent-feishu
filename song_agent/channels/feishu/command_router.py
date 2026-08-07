"""飞书消息的确定性命令路由（Q8）。

命令层位于 Channel 输入链路的最前端，**先于附件解析**：
- /help /status /clear
- 提醒状态问答
- 纯文字“确认”拦截

命中的命令不进入 IntentExtractor / Dispatcher / Agent，也不写 Conversation
Context。业务状态操作（/status /clear /提醒状态）下沉到 `PlanModule`，
本路由不直接访问 Store。
"""

from __future__ import annotations

import re

from ...domain.results import ApplicationResult
from ...models import IncomingMessage
from ...modules.plans.service import PlanModule
from ...planner import is_clear_command, is_confirmation, is_reminder_status_question
from .texts import TEXT_CONFIRMATION_DISABLED, help_message


class FeishuCommandRouter:
    def __init__(self, plans: PlanModule, *, app_id: str = "") -> None:
        self.plans = plans
        self.app_id = app_id

    async def try_handle(
        self,
        message: IncomingMessage,
        text: str,
        *,
        date: str,
    ) -> ApplicationResult | None:
        if re.match(r"^/(help|帮助)\b", text, re.IGNORECASE):
            return ApplicationResult(status="ok", intent="plans.help", message=help_message())

        identity = message.identity(self.app_id or message.app_id)

        if re.match(r"^/(status|状态)\b", text, re.IGNORECASE):
            return await self.plans.get_today_status(
                identity,
                message.chat_id,
                message.thread_id or message.root_id,
                date=date,
            )

        if is_clear_command(text):
            return await self.plans.clear_today(
                identity,
                message.chat_id,
                message.thread_id or message.root_id,
                date=date,
            )

        if is_reminder_status_question(text):
            return await self.plans.get_reminder_status(
                identity,
                message.chat_id,
                message.thread_id or message.root_id,
                date=date,
            )

        if is_confirmation(text):
            return ApplicationResult(
                status="ok",
                intent="plans.confirmation_blocked",
                message=TEXT_CONFIRMATION_DISABLED,
            )

        return None
