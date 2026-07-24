"""飞书交互式卡片，用于持久化敏感操作确认。"""

from __future__ import annotations

from typing import Any

from ..models import PendingAction


def calendar_confirmation_card(markdown: str, action: PendingAction) -> dict[str, Any]:
    return confirmation_card(
        markdown,
        action,
        header="待确认：创建个人日历",
        confirm_title="确认创建日程？",
        confirm_text="将使用你自己的 OAuth 授权写入个人日历。",
        button_text="确认创建",
    )


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
    value = {
        "action_id": action.action_id,
        "payload_hash": action.payload_hash,
    }
    return {
        "config": {"wide_screen_mode": True, "enable_forward": False},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": header},
        },
        "elements": [
            {"tag": "markdown", "content": markdown},
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": "仅创建者可操作，30 分钟后失效；重复点击不会重复执行。",
                    }
                ],
            },
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "type": "primary",
                        "text": {"tag": "plain_text", "content": button_text},
                        "value": {**value, "decision": "confirm"},
                        "confirm": {
                            "title": {"tag": "plain_text", "content": confirm_title},
                            "text": {
                                "tag": "plain_text",
                                "content": confirm_text,
                            },
                        },
                    },
                    {
                        "tag": "button",
                        "type": "default",
                        "text": {"tag": "plain_text", "content": "取消"},
                        "value": {**value, "decision": "cancel"},
                    },
                ],
            },
        ],
    }
