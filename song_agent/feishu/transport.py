"""
飞书消息传输模块。

通过飞书 WebSocket 长连接接收群聊和私聊消息，支持发送 Markdown 卡片。
自动处理群聊绑定和消息解析。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from collections.abc import Awaitable, Callable
from typing import Any

import lark_oapi as lark
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody, P2ImMessageReceiveV1
from lark_oapi.ws import Client as WsClient

from ..config import Settings
from ..models import IncomingMessage
from ..store import JsonStore

MessageHandler = Callable[[IncomingMessage], Awaitable[None]]


class FeishuTransport:
    """
    飞书消息传输器。

    通过 WebSocket 长连接接收消息，支持发送 Markdown 卡片回复。
    自动处理群聊绑定和消息解析。
    """

    def __init__(self, settings: Settings, store: JsonStore) -> None:
        self.settings = settings
        self.store = store
        self.logger = logging.getLogger(__name__)
        self.group_ids = settings.allowed_group_chat_ids | store.group_chat_ids()
        builder = lark.Client.builder().app_id(settings.feishu_app_id).app_secret(settings.feishu_app_secret)
        self.client = builder.domain(settings.domain).build()
        self._thread: threading.Thread | None = None

    def start(self, loop: asyncio.AbstractEventLoop, handler: MessageHandler) -> None:
        def on_event(event: P2ImMessageReceiveV1) -> None:
            future = asyncio.run_coroutine_threadsafe(self._dispatch(event, handler), loop)
            future.add_done_callback(self._log_future_error)

        dispatcher = (
            lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(on_event).build()
        )
        ws = WsClient(
            self.settings.feishu_app_id,
            self.settings.feishu_app_secret,
            event_handler=dispatcher,
            domain=self.settings.domain,
            log_level=lark.LogLevel.INFO,
        )
        self._thread = threading.Thread(target=ws.start, name="feishu-ws", daemon=True)
        self._thread.start()
        self.logger.info("飞书 WebSocket 长连接已启动")

    async def send_markdown(self, chat_id: str, markdown: str) -> str | None:
        self.logger.info("📤 管家返回消息 chat=%s content=%r", chat_id, markdown)
        card = {
            "schema": "2.0",
            "config": {"update_multi": True},
            "body": {
                "direction": "vertical",
                "padding": "12px 12px 12px 12px",
                "elements": [{"tag": "markdown", "content": markdown, "text_align": "left"}],
            },
        }
        body = (
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("interactive")
            .content(json.dumps(card, ensure_ascii=False))
            .build()
        )
        request = CreateMessageRequest.builder().receive_id_type("chat_id").request_body(body).build()
        response = await asyncio.to_thread(self.client.im.v1.message.create, request)
        if not response.success():
            raise RuntimeError(f"发送飞书消息失败: {response.code} {response.msg}")
        message_id = response.data.message_id if response.data else None
        self.logger.info("✅ 消息发送成功 chat=%s message_id=%s", chat_id, message_id)
        return message_id

    async def _dispatch(self, event: P2ImMessageReceiveV1, handler: MessageHandler) -> None:
        try:
            data = event.event
            sender = data.sender if data else None
            message = data.message if data else None
            user_id = sender.sender_id.open_id if sender and sender.sender_id else ""
            if not message or not user_id or not message.chat_id or not message.message_id:
                return
            if message.chat_type not in {"p2p", "group"}:
                return
            if message.chat_type == "group":
                if message.chat_id not in self.group_ids:
                    if self.group_ids or (
                        self.settings.admin_user_ids and user_id not in self.settings.admin_user_ids
                    ):
                        self.logger.warning("忽略未绑定群聊消息", extra={"chat_id": message.chat_id})
                        return
                    self.group_ids.add(message.chat_id)
                    await self.store.add_group_chat_id(message.chat_id)
                    self.logger.info("已由管理员首次艾特绑定群聊 %s", message.chat_id)
            else:
                await self.store.save_p2p_chat_id(user_id, message.chat_id)
            text = parse_message_text(message.message_type or "", message.content or "")
            await handler(
                IncomingMessage(
                    message_id=message.message_id,
                    user_id=user_id,
                    chat_id=message.chat_id,
                    chat_type=message.chat_type,
                    message_type=message.message_type or "",
                    text=text,
                )
            )
        except Exception:
            self.logger.exception("处理飞书消息事件失败")

    def _log_future_error(self, future: Any) -> None:
        try:
            future.result()
        except Exception:
            self.logger.exception("飞书消息异步任务失败")


def parse_message_text(message_type: str, content: str) -> str:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return ""
    if message_type == "text":
        value = payload.get("text") if isinstance(payload, dict) else None
        return value.strip() if isinstance(value, str) else ""
    if message_type == "post":
        return "\n".join(_flatten_post(payload)).strip()
    return ""


def clean_incoming_text(text: str) -> str:
    value = re.sub(r"@_user_\d+", "", text)
    value = re.sub(r"[\u200b-\u200f\u2060\ufeff]", "", value)
    return value.strip()


def _flatten_post(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for child in value for item in _flatten_post(child)]
    if not isinstance(value, dict):
        return []
    result = [value["text"]] if isinstance(value.get("text"), str) else []
    metadata = {"text", "tag", "style", "href", "user_id", "user_name", "image_key"}
    for key, child in value.items():
        if key not in metadata:
            result.extend(_flatten_post(child))
    return result
