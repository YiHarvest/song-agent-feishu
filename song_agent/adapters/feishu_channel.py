from __future__ import annotations

import hashlib
import hmac
import io
import json
from collections.abc import Mapping
from typing import Any

import httpx

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
    """Trusted Feishu webhook boundary and channel-specific presenter."""

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

    async def handle(
        self,
        payload: dict[str, Any],
        *,
        raw_body: bytes,
        headers: Mapping[str, str],
    ) -> dict[str, Any]:
        self._verify(payload, raw_body=raw_body, headers=headers)
        if payload.get("challenge"):
            return {"challenge": payload["challenge"]}
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
