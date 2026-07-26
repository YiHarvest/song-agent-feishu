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
import lark_oapi.ws.client as lark_ws_client
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody, P2ImMessageReceiveV1
from lark_oapi.ws import Client as WsClient

from ..config import Settings
from ..feishu.cards import business_confirmation_card, document_confirmation_card
from ..models import IncomingMessage, PendingAction
from ..store import SqliteStore

MessageHandler = Callable[[IncomingMessage], Awaitable[None]]


class FeishuTransport:
    """
    飞书消息传输器。

    通过 WebSocket 长连接接收消息，支持发送 Markdown 卡片回复。
    自动处理群聊绑定和消息解析。
    """

    def __init__(self, settings: Settings, store: SqliteStore) -> None:
        self.settings = settings
        self.store = store
        self.logger = logging.getLogger(__name__)
        self.group_ids = set(settings.allowed_group_chat_ids)
        builder = lark.Client.builder().app_id(settings.feishu_app_id).app_secret(settings.feishu_app_secret)
        self.client = builder.domain(settings.domain).build()
        self._thread: threading.Thread | None = None

    async def initialize(self) -> None:
        self.group_ids.update(await self.store.group_chat_ids(app_id=self.settings.feishu_app_id))

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
            # INFO 级别会记录完整的 WebSocket URL，包括短期访问密钥和票据。
            # 保持 SDK 日志级别为 WARNING，使用我们自己的生命周期消息替代。
            log_level=lark.LogLevel.WARNING,
        )
        self._thread = threading.Thread(target=_run_ws_client, args=(ws,), name="feishu-ws", daemon=True)
        self._thread.start()
        self.logger.info("飞书 WebSocket 长连接已启动")

    async def send_markdown(self, chat_id: str, markdown: str) -> str | None:
        self.logger.info("📤 管家返回消息 chat=%s content=%r", chat_id, markdown)
        card = {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "body": {
                "elements": [{"tag": "markdown", "content": markdown}],
            },
        }
        return await self.send_card(chat_id, card)

    async def send_confirmation_card(
        self,
        chat_id: str,
        markdown: str,
        action: PendingAction,
    ) -> str | None:
        card = (
            document_confirmation_card(markdown, action)
            if action.action_type.startswith("document.")
            else business_confirmation_card(markdown, action)
        )
        message_id = await self.send_card(chat_id, card)
        if message_id:
            await self.store.set_pending_action_card_message(action.action_id, message_id)
        return message_id

    async def send_card(self, chat_id: str, card: dict[str, Any]) -> str | None:
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
        self.logger.info("✅ 卡片发送成功 chat=%s message_id=%s", chat_id, message_id)
        return message_id

    async def _dispatch(self, event: P2ImMessageReceiveV1, handler: MessageHandler) -> None:
        try:
            data = event.event
            sender = data.sender if data else None
            message = data.message if data else None
            sender_id = sender.sender_id if sender else None
            open_id = getattr(sender_id, "open_id", "") or ""
            tenant_user_id = getattr(sender_id, "user_id", "") or ""
            union_id = getattr(sender_id, "union_id", "") or ""
            subject_id = union_id or tenant_user_id or open_id
            header = getattr(event, "header", None)
            tenant_key = getattr(header, "tenant_key", "") or ""
            event_id = getattr(header, "event_id", "") or ""
            if not message or not subject_id or not open_id or not message.chat_id or not message.message_id:
                return
            if message.chat_type not in {"p2p", "group"}:
                return
            if message.chat_type == "group":
                if message.chat_id not in self.group_ids:
                    if self.group_ids or (
                        self.settings.admin_user_ids and open_id not in self.settings.admin_user_ids
                    ):
                        self.logger.warning("忽略未绑定群聊消息", extra={"chat_id": message.chat_id})
                        return
                    self.group_ids.add(message.chat_id)
                    await self.store.add_group_chat_id(
                        message.chat_id,
                        tenant_key=tenant_key,
                        app_id=self.settings.feishu_app_id,
                    )
                    self.logger.info("已由管理员首次艾特绑定群聊 %s", message.chat_id)
            else:
                await self.store.save_p2p_chat_id(
                    subject_id,
                    message.chat_id,
                    tenant_key=tenant_key,
                    app_id=self.settings.feishu_app_id,
                )
            text = parse_message_text(message.message_type or "", message.content or "")
            await handler(
                IncomingMessage(
                    message_id=message.message_id,
                    event_id=event_id or message.message_id,
                    tenant_key=tenant_key,
                    app_id=self.settings.feishu_app_id,
                    user_id=subject_id,
                    open_id=open_id,
                    tenant_user_id=tenant_user_id,
                    union_id=union_id,
                    chat_id=message.chat_id,
                    thread_id=getattr(message, "thread_id", "") or "",
                    root_id=getattr(message, "root_id", "") or "",
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


def _run_ws_client(ws: WsClient) -> None:
    """在 WebSocket 线程拥有的循环上运行 SDK。

    lark-oapi 在导入时将其循环存储在模块全局变量中。当从 FastAPI 导入时，
    那是已经运行的 uvloop，因此从另一个线程调用 ``start`` 会在 SDK 重连前失败一次。
    Song Agent 每个进程只有一个飞书连接，因此在这里重新绑定 SDK 循环，
    保持两个事件循环隔离。
    """

    ws_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(ws_loop)
    lark_ws_client.loop = ws_loop
    try:
        ws.start()
    finally:
        pending = asyncio.all_tasks(ws_loop)
        for task in pending:
            task.cancel()
        if pending:
            ws_loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        ws_loop.close()
        asyncio.set_event_loop(None)


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
