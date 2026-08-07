"""飞书卡片 JSON 2.0 模板（纯构造函数，无副作用）。

只依赖 `PendingAction` 领域对象；卡片 action value 仅携带
`action_name` + `action_id`，绝不携带 payload / token / 正文。
"""

from __future__ import annotations

from typing import Any

from ...models import PendingAction


def calendar_confirmation_card(markdown: str, action: PendingAction) -> dict[str, Any]:
    return business_confirmation_card(markdown, action)


def business_confirmation_card(markdown: str, action: PendingAction) -> dict[str, Any]:
    operation = {
        "calendar.create": ("创建个人日程", "创建日程"),
        "calendar.update": ("修改个人日程", "修改日程"),
        "calendar.delete": ("删除个人日程", "删除日程"),
        "reminder.create": ("创建提醒", "创建提醒"),
        "reminder.cancel": ("取消提醒", "取消提醒"),
        "task.create": ("创建任务", "创建任务"),
        "task.update": ("修改任务", "修改任务"),
        "task.complete": ("完成任务", "完成任务"),
        "task.delete": ("删除任务", "删除任务"),
    }.get(action.action_type, ("执行操作", "执行"))
    return confirmation_card(
        markdown,
        action,
        header=f"待确认：{operation[0]}",
        confirm_title=f"确认{operation[1]}？",
        confirm_text="将使用你自己的 OAuth 授权执行该操作。",
        button_text=f"确认{operation[1]}",
    )


def action_confirmation_markdown(action: PendingAction) -> str:
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


def document_confirmation_card(markdown: str, action: PendingAction) -> dict[str, Any]:
    operation = "创建" if action.action_type == "document.create" else "追加"
    return confirmation_card(
        markdown,
        action,
        header=f"待确认：{operation}飞书云文档",
        confirm_title=f"确认{operation}文档？",
        confirm_text="将使用你自己的 OAuth 授权写入飞书云文档。",
        button_text=f"确认{operation}",
    )


def confirmation_card(
    markdown: str,
    action: PendingAction,
    *,
    header: str,
    confirm_title: str,
    confirm_text: str,
    button_text: str,
) -> dict[str, Any]:
    return {
        "schema": "2.0",
        "config": {
            "update_multi": True,
            "width_mode": "fill",
        },
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": header},
        },
        "body": {
            "direction": "vertical",
            "elements": [
                {"tag": "markdown", "content": markdown},
                {
                    "tag": "markdown",
                    "content": (
                        "<font color='grey'>仅创建者可操作，30 分钟后失效；"
                        "重复点击不会重复执行。</font>"
                    ),
                },
                {
                    "tag": "column_set",
                    "horizontal_spacing": "8px",
                    "columns": [
                        {
                            "tag": "column",
                            "width": "auto",
                            "elements": [
                                {
                                    "tag": "button",
                                    "type": "primary",
                                    "text": {"tag": "plain_text", "content": button_text},
                                    "value": {
                                        "action": "pending_action.confirm",
                                        "action_id": action.action_id,
                                    },
                                    "confirm": {
                                        "title": {
                                            "tag": "plain_text",
                                            "content": confirm_title,
                                        },
                                        "text": {
                                            "tag": "plain_text",
                                            "content": confirm_text,
                                        },
                                    },
                                }
                            ],
                        },
                        {
                            "tag": "column",
                            "width": "auto",
                            "elements": [
                                {
                                    "tag": "button",
                                    "type": "default",
                                    "text": {"tag": "plain_text", "content": "取消"},
                                    "value": {
                                        "action": "pending_action.cancel",
                                        "action_id": action.action_id,
                                    },
                                }
                            ],
                        },
                    ],
                },
            ],
        },
    }
