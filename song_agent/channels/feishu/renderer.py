"""纯渲染器：把领域对象转换成可发送内容。

不访问 Store / Repository / OAuth / Outbox / Service / Transport；
不执行异步操作，不写日志，不发请求。
"""

from __future__ import annotations

from typing import Any

from ...domain.results import ApplicationResult
from ...models import PendingAction
from .cards import (
    business_confirmation_card,
    document_confirmation_card,
)


class FeishuRenderer:
    """只做输入 -> Markdown / Card JSON 的纯转换。"""

    def render_result(self, result: ApplicationResult) -> str:
        if result.status == "authorization_required" and result.authorization_url:
            return f"{result.message}[点击这里完成授权]({result.authorization_url})"
        return result.message

    def render_confirmation(self, action: PendingAction) -> dict[str, Any]:
        if action.action_type.startswith("document."):
            return document_confirmation_card(
                action_confirmation_summary(action),
                action,
            )
        return business_confirmation_card(
            action_confirmation_summary(action),
            action,
        )


def action_confirmation_summary(action: PendingAction) -> str:
    """从 Action payload 构造展示摘要（不含敏感/完整正文）。"""
    payload = action.payload
    lines: list[str] = []
    for label, key in (
        ("标题", "summary"),
        ("开始", "start_time"),
        ("结束", "end_time"),
        ("截止", "due_time"),
        ("日程 ID", "event_id"),
        ("任务 GUID", "task_guid"),
    ):
        if payload.get(key) not in (None, ""):
            lines.append(f"**{label}：** {payload[key]}")
    if isinstance(payload.get("fields"), dict):
        lines.append(f"**修改字段：** {', '.join(payload['fields'])}")
    return "\n\n".join(lines) or f"**操作：** {action.action_type}"
