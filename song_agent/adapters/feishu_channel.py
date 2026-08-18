from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import io
import json
import logging
import threading
from collections.abc import Mapping
from typing import Any

import httpx
import lark_oapi as lark
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
from lark_oapi.ws import Client as WsClient

from ..kernel.errors import DeliveryState, ProviderError
from ..kernel.models import (
    AttachmentPart,
    ConversationRef,
    DeliveryTarget,
    IntentResult,
    NaturalLanguagePayload,
    PrincipalIdentity,
    RequestEnvelope,
    TextPart,
)
from ..ports.attachments import AttachmentStorePort
from ..ports.repositories import EventDedupRepository
from ..runtime.actions import ActionService
from ..runtime.coordinator import ExecutionCoordinator
from ..runtime.dispatcher import IntentDispatcher
from ..runtime.router import TopLevelRouter


class FeishuChannelAdapter:
    """Trusted Feishu webhook boundary and channel-specific presenter.

    Supports both HTTP webhook and WebSocket long-polling for receiving messages.
    """

    channel_id = "feishu"

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        base_url: str,
        verification_token: str,
        encrypt_key: str,
        permissions: frozenset[str],
        router: TopLevelRouter,
        dispatcher: IntentDispatcher,
        actions: ActionService,
        dedup: EventDedupRepository,
        attachments: AttachmentStorePort,
        coordinator: ExecutionCoordinator,
        timeout: float = 30,
        allowed_group_ids: set[str] | None = None,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.base_url = base_url.rstrip("/")
        self.verification_token = verification_token
        self.encrypt_key = encrypt_key
        self.permissions = permissions
        self.router = router
        self.dispatcher = dispatcher
        self.actions = actions
        self.dedup = dedup
        self.attachments = attachments
        self.coordinator = coordinator
        self.timeout = timeout
        self.transport: httpx.AsyncBaseTransport | None = None

        # WebSocket state
        self._logger = logging.getLogger(__name__)
        self._ws_thread: threading.Thread | None = None
        self._ws_accepting = False
        self._ws_futures: set[Any] = set()
        self._ws_futures_lock = threading.Lock()

        # Group chat whitelist (auto-registered on first message)
        self.group_ids: set[str] = allowed_group_ids or set()

    def _decrypt(self, encrypt_data: str) -> dict[str, Any]:
        """Decrypt Feishu encrypted payload using AES-256-CBC.

        Feishu uses SHA-256 hash of the encrypt_key as the AES key.
        """
        if not self.encrypt_key:
            raise ValueError("encrypt_key is required for encrypted payloads")

        # Decode base64
        encrypted = base64.b64decode(encrypt_data)

        # Key is SHA-256 hash of the encrypt_key
        key = hashlib.sha256(self.encrypt_key.encode('utf-8')).digest()

        # First 16 bytes are IV, rest is ciphertext
        iv = encrypted[:16]
        ciphertext = encrypted[16:]

        # Decrypt using AES-256-CBC with cryptography library
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
        decryptor = cipher.decryptor()
        decrypted = decryptor.update(ciphertext) + decryptor.finalize()

        # Remove PKCS7 padding
        pad_len = decrypted[-1]
        decrypted = decrypted[:-pad_len]

        return json.loads(decrypted.decode('utf-8'))

    async def handle(
        self,
        payload: dict[str, Any],
        *,
        raw_body: bytes,
        headers: Mapping[str, str],
    ) -> dict[str, Any]:
        # Decrypt if payload is encrypted
        if payload.get("encrypt"):
            payload = self._decrypt(payload["encrypt"])

        # URL verification (challenge) - only verify token, skip signature
        if payload.get("challenge"):
            # Token can be in payload["token"] or payload["header"]["token"]
            header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
            supplied_token = str(header.get("token") or payload.get("token") or "")
            if not self.verification_token or not hmac.compare_digest(
                supplied_token, self.verification_token
            ):
                raise PermissionError("invalid Feishu verification token")
            return {"challenge": payload["challenge"]}
        # Regular events - full verification including signature
        self._verify(payload, raw_body=raw_body, headers=headers)
        header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
        event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
        event_id = str(header.get("event_id") or payload.get("event_id") or "")
        if not event_id:
            raise ValueError("Feishu event has no event_id")
        if not await self.dedup.claim(event_id, source="feishu"):
            return {"ok": True, "duplicate": True}
        if isinstance(event.get("action"), dict):
            return await self._handle_action(event, header)
        return await self._handle_message(event, header, event_id)

    async def _handle_message(
        self,
        event: dict[str, Any],
        header: dict[str, Any],
        event_id: str,
    ) -> dict[str, Any]:
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
        sender_id = sender.get("sender_id") if isinstance(sender.get("sender_id"), dict) else {}
        open_id = str(sender_id.get("open_id") or "")
        tenant_id = str(header.get("tenant_key") or "default")
        chat_id = str(message.get("chat_id") or "")
        message_id = str(message.get("message_id") or event_id)
        if not open_id or not chat_id:
            raise ValueError("Feishu message lacks a trusted sender or chat id")
        principal = self._principal(tenant_id, open_id)
        target = DeliveryTarget(
            channel="feishu",
            channel_account_id=self.app_id,
            destination_id=chat_id,
            thread_id=str(message.get("thread_id") or ""),
        )
        conversation = ConversationRef(
            tenant_id=tenant_id,
            channel="feishu",
            channel_account_id=self.app_id,
            conversation_id=chat_id,
            principal_id=open_id,
            thread_id=target.thread_id,
        )
        parts = await self._content_parts(message, principal, conversation)
        envelope = RequestEnvelope(
            request_id=message_id,
            principal=principal,
            delivery_target=target,
            conversation=conversation,
            source="feishu",
            payload=NaturalLanguagePayload(content=parts),
            metadata={"trace_id": event_id},
        )
        async with self.coordinator.lock_for(envelope):
            routed = await self.router.route(envelope)
            result = routed.result
            if routed.envelope is not None:
                result = await self.dispatcher.execute(routed.envelope)
        if result is None:
            raise RuntimeError("router returned neither envelope nor result")
        await self.present(target, result, request=envelope)
        return {"ok": True}

    async def _handle_action(
        self,
        event: dict[str, Any],
        header: dict[str, Any],
    ) -> dict[str, Any]:
        operator = event.get("operator") if isinstance(event.get("operator"), dict) else {}
        open_id = str(operator.get("open_id") or operator.get("operator_id") or "")
        principal = self._principal(str(header.get("tenant_key") or "default"), open_id)
        action = event["action"]
        value = action.get("value") if isinstance(action.get("value"), dict) else {}
        action_id = str(value.get("action_id") or "")
        operation = str(value.get("operation") or "confirm")
        if not action_id or operation not in {"confirm", "cancel"}:
            raise ValueError("invalid Feishu action callback")
        if operation == "confirm":
            result = await self.actions.confirm(action_id, principal)
        else:
            result = await self.actions.cancel(action_id, principal)
        return {
            "toast": {
                "type": "success" if result.status == "success" else "warning",
                "content": result.message or result.code,
            }
        }

    async def _content_parts(
        self,
        message: dict[str, Any],
        principal: PrincipalIdentity,
        conversation: ConversationRef,
    ) -> tuple[TextPart | AttachmentPart, ...]:
        message_type = str(message.get("message_type") or "text")
        try:
            content = json.loads(str(message.get("content") or "{}"))
        except json.JSONDecodeError as error:
            raise ValueError("invalid Feishu message content") from error
        if message_type in {"text", "post"}:
            text = str(content.get("text") or _post_text(content)).strip()
            if not text:
                raise ValueError("Feishu message contains no text")
            return (TextPart(text=text),)
        key = str(content.get("file_key") or content.get("image_key") or "")
        if message_type not in {"image", "file", "audio", "media"} or not key:
            raise ValueError(f"unsupported Feishu message type: {message_type}")
        data, media_type = await self._download_resource(
            str(message.get("message_id") or ""), key, message_type
        )
        filename = str(content.get("file_name") or f"{key}.{_extension(message_type)}")
        attachment = await self.attachments.save(
            principal=principal,
            conversation=conversation,
            filename=filename,
            media_type=media_type,
            stream=io.BytesIO(data),
            temporary=True,
        )
        return (AttachmentPart(attachment=attachment),)

    async def present(
        self,
        target: DeliveryTarget,
        result: IntentResult,
        *,
        request: RequestEnvelope,
    ) -> str | None:
        if result.status == "confirmation_required":
            content = {
                "schema": "2.0",
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": result.presentation_hints.title or "确认操作",
                    }
                },
                "body": {
                    "elements": [
                        {"tag": "markdown", "content": result.message or result.code},
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "确认"},
                            "type": "danger",
                            "value": {
                                "operation": "confirm",
                                "action_id": result.data["action_id"],
                            },
                        },
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "取消"},
                            "value": {
                                "operation": "cancel",
                                "action_id": result.data["action_id"],
                            },
                        },
                    ]
                },
            }
            message_type = "interactive"
        else:
            content = {"text": result.message or json.dumps(result.data, ensure_ascii=False)}
            message_type = "text"
        data, _ = await self._bot_request(
            "POST",
            "/open-apis/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            json_body={
                "receive_id": target.destination_id,
                "msg_type": message_type,
                "content": json.dumps(content, ensure_ascii=False),
                "uuid": request.request_id,
            },
            write=True,
        )
        return str(data.get("message_id") or "")

    async def _download_resource(
        self, message_id: str, resource_key: str, message_type: str
    ) -> tuple[bytes, str]:
        token = await self._tenant_access_token()
        resource_type = "image" if message_type == "image" else "file"
        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout,
            transport=self.transport,
            trust_env=False,
        ) as client:
            response = await client.get(
                (
                    f"/open-apis/im/v1/messages/{message_id}/resources/"
                    f"{resource_key}"
                ),
                params={"type": resource_type},
                headers={"Authorization": f"Bearer {token}"},
            )
        if response.is_error:
            raise ProviderError(
                "feishu.attachment_download_failed",
                "Unable to download the Feishu attachment.",
                delivery_state=DeliveryState.NOT_SENT,
            )
        return response.content, response.headers.get("content-type", "application/octet-stream")

    async def _bot_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        write: bool = False,
    ) -> tuple[dict[str, Any], str]:
        token = await self._tenant_access_token()
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                transport=self.transport,
                trust_env=False,
            ) as client:
                response = await client.request(
                    method,
                    path,
                    params=params,
                    json=json_body,
                    headers={"Authorization": f"Bearer {token}"},
                )
        except (httpx.ConnectError, httpx.ConnectTimeout) as error:
            raise ProviderError(
                "feishu.channel_not_sent",
                "Unable to connect to Feishu.",
                retryable=True,
                delivery_state=DeliveryState.NOT_SENT,
            ) from error
        except (httpx.ReadTimeout, httpx.RemoteProtocolError) as error:
            raise ProviderError(
                "feishu.channel_delivery_unknown",
                "Feishu did not return a verifiable delivery response.",
                delivery_state=DeliveryState.UNKNOWN if write else DeliveryState.NOT_SENT,
            ) from error
        request_id = response.headers.get("x-tt-logid", "")
        payload = response.json() if response.content else {}
        code = payload.get("code") or (response.status_code if response.is_error else 0)
        if response.is_error or code:
            raise ProviderError(
                f"feishu.{code}",
                str(payload.get("msg") or "Feishu channel request failed."),
                delivery_state=DeliveryState.REJECTED,
                provider_request_id=request_id,
            )
        return payload.get("data") or {}, request_id

    async def _tenant_access_token(self) -> str:
        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout,
            transport=self.transport,
            trust_env=False,
        ) as client:
            response = await client.post(
                "/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self.app_id, "app_secret": self.app_secret},
            )
        payload = response.json() if response.content else {}
        token = str(payload.get("tenant_access_token") or "")
        if response.is_error or payload.get("code") or not token:
            raise ProviderError(
                "feishu.channel_auth_failed",
                "Unable to authenticate the Feishu channel adapter.",
                delivery_state=DeliveryState.NOT_SENT,
            )
        return token

    def _verify(
        self,
        payload: dict[str, Any],
        *,
        raw_body: bytes,
        headers: Mapping[str, str],
    ) -> None:
        header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
        supplied_token = str(header.get("token") or payload.get("token") or "")
        if not self.verification_token or not hmac.compare_digest(
            supplied_token, self.verification_token
        ):
            raise PermissionError("invalid Feishu verification token")
        if self.encrypt_key:
            timestamp = headers.get("x-lark-request-timestamp", "")
            nonce = headers.get("x-lark-request-nonce", "")
            signature = headers.get("x-lark-signature", "")
            expected = hashlib.sha256(
                timestamp.encode() + nonce.encode() + self.encrypt_key.encode() + raw_body
            ).hexdigest()
            if not signature or not hmac.compare_digest(signature, expected):
                raise PermissionError("invalid Feishu request signature")

    def _principal(self, tenant_id: str, open_id: str) -> PrincipalIdentity:
        if not open_id:
            raise ValueError("Feishu principal has no open_id")
        return PrincipalIdentity(
            tenant_id=tenant_id,
            principal_id=open_id,
            auth_provider="feishu_event",
            attributes={"permissions": ",".join(sorted(self.permissions))},
        )

    # ==========================================================================
    # WebSocket long-polling support
    # ==========================================================================

    def start_websocket(self, loop: asyncio.AbstractEventLoop) -> None:
        """Start WebSocket connection to receive messages via long-polling.

        This allows receiving messages without configuring event push in Feishu admin.
        """
        self._ws_accepting = True

        def on_event(event: P2ImMessageReceiveV1) -> None:
            with self._ws_futures_lock:
                if not self._ws_accepting:
                    return
                future = asyncio.run_coroutine_threadsafe(
                    self._dispatch_ws_event(event),
                    loop,
                )
                self._ws_futures.add(future)
            future.add_done_callback(self._finish_ws_future)

        dispatcher = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(on_event)
            .build()
        )
        ws = WsClient(
            self.app_id,
            self.app_secret,
            event_handler=dispatcher,
            domain=self.base_url,
            log_level=lark.LogLevel.WARNING,
        )
        self._ws_thread = threading.Thread(
            target=_run_ws_client,
            args=(ws,),
            name="feishu-ws",
            daemon=True,
        )
        self._ws_thread.start()
        self._logger.info("飞书 WebSocket 长连接已启动")

    async def close_websocket(self, *, timeout_seconds: float = 30) -> None:
        """Close WebSocket connection."""
        with self._ws_futures_lock:
            self._ws_accepting = False
            futures = list(self._ws_futures)
        if not futures:
            return
        wrapped = [asyncio.wrap_future(future) for future in futures]
        _, pending = await asyncio.wait(wrapped, timeout=timeout_seconds)
        if pending:
            self._logger.warning("关闭时取消 %d 个未完成飞书消息任务", len(pending))
            for future in futures:
                if not future.done():
                    future.cancel()
            await asyncio.gather(*wrapped, return_exceptions=True)

    async def _dispatch_ws_event(self, event: P2ImMessageReceiveV1) -> None:
        """Dispatch WebSocket message event to handler."""
        try:
            data = event.event
            sender = data.sender if data else None
            message = data.message if data else None
            sender_id = sender.sender_id if sender else None
            open_id = getattr(sender_id, "open_id", "") or ""
            tenant_user_id = getattr(sender_id, "user_id", "") or ""
            union_id = getattr(sender_id, "union_id", "") or ""

            header = getattr(event, "header", None)
            tenant_key = getattr(header, "tenant_key", "") or ""
            event_id = getattr(header, "event_id", "") or ""

            if not message or not open_id or not message.chat_id or not message.message_id:
                return

            chat_type = message.chat_type or ""
            if chat_type not in {"p2p", "group"}:
                return

            # Auto-register group chats
            if chat_type == "group":
                if message.chat_id not in self.group_ids:
                    self.group_ids.add(message.chat_id)
                    self._logger.info(
                        "已自动登记群聊 chat_id=%s first_sender=%s",
                        message.chat_id,
                        open_id,
                    )

            message_type = message.message_type or ""
            raw_content = message.content or ""

            self._logger.info(
                "📥 飞书消息事件 (WebSocket) message_id=%s event_id=%s chat=%s "
                "chat_type=%s message_type=%s content_chars=%d",
                message.message_id,
                event_id or message.message_id,
                message.chat_id,
                chat_type,
                message_type,
                len(raw_content),
            )

            # Build event dict for reuse with existing handler
            event_dict = {
                "message": {
                    "chat_id": message.chat_id,
                    "message_id": message.message_id,
                    "message_type": message_type,
                    "content": raw_content,
                    "chat_type": chat_type,
                    "thread_id": getattr(message, "thread_id", "") or "",
                },
                "sender": {
                    "sender_id": {
                        "open_id": open_id,
                        "user_id": tenant_user_id,
                        "union_id": union_id,
                    }
                },
            }
            header_dict = {
                "tenant_key": tenant_key,
                "event_id": event_id,
            }

            # Dedup check
            if not await self.dedup.claim(event_id or message.message_id, source="feishu"):
                self._logger.debug("重复消息，跳过")
                return

            # Handle message using existing logic (supports text, post, audio, image, file, media)
            result = await self._handle_message(event_dict, header_dict, event_id or message.message_id)
            self._logger.debug("消息处理完成: %s", result)

        except Exception:
            self._logger.exception("处理飞书 WebSocket 消息事件失败")

    def _finish_ws_future(self, future: Any) -> None:
        """Callback when a WebSocket future completes."""
        with self._ws_futures_lock:
            self._ws_futures.discard(future)
        try:
            future.result()
        except Exception:
            self._logger.exception("飞书 WebSocket 消息异步任务失败")


def _post_text(content: dict[str, Any]) -> str:
    values: list[str] = []
    for locale in content.values():
        if not isinstance(locale, dict):
            continue
        if locale.get("title"):
            values.append(str(locale["title"]))
        for paragraph in locale.get("content") or []:
            for element in paragraph if isinstance(paragraph, list) else []:
                if isinstance(element, dict) and element.get("text"):
                    values.append(str(element["text"]))
    return "\n".join(values)


def _extension(message_type: str) -> str:
    return {"image": "img", "audio": "audio", "media": "media"}.get(
        message_type, "bin"
    )


def _run_ws_client(ws: WsClient) -> None:
    """Run WebSocket client in its own thread.

    lark-oapi stores its event loop in a module global variable. When imported
    from FastAPI, that's an already-running uvloop, so calling ``start`` from
    another thread would fail once before SDK reconnects. Song Agent has only
    one Feishu connection per process, so rebind the SDK loop here to keep
    both event loops isolated.
    """
    import lark_oapi.ws.client as lark_ws_client

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


def _parse_message_text(message_type: str, content: str) -> str:
    """Parse text content from Feishu message."""
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


def _flatten_post(value: Any) -> list[str]:
    """Flatten Feishu post message content to text."""
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
