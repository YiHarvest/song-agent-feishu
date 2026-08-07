"""Protocol adapter from OpenAI Chat Completions to the shared ApplicationDispatcher."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, status

from ..api.agent_auth import ApiCredential, api_error
from ..api.agent_schemas import ChatCompletionRequest
from ..config import Settings
from ..domain.intents import UserRequest
from ..domain.results import ApplicationResult
from ..models import ApiChannelBinding, FeishuIdentity
from ..store import SqliteStore
from .dispatcher import ApplicationDispatcher
from .result_renderer import render_message

_SUPPORTED_FIELDS = {
    "model",
    "messages",
    "stream",
    "stream_options",
    "temperature",
    "top_p",
    "max_tokens",
    "max_completion_tokens",
    "stop",
    "n",
    "metadata",
    "user",
}
_SUPPORTED_ROLES = {"system", "developer", "user", "assistant"}


@dataclass(frozen=True, slots=True)
class ResolvedApiIdentity:
    identity: FeishuIdentity
    chat_id: str
    principal_id: str
    binding: ApiChannelBinding | None = None


class OpenAIAdapter:
    def __init__(
        self,
        settings: Settings,
        store: SqliteStore,
        dispatcher: ApplicationDispatcher,
    ) -> None:
        self.settings = settings
        self.store = store
        self.dispatcher = dispatcher
        self.logger = logging.getLogger(__name__)

    async def complete(
        self,
        payload: ChatCompletionRequest,
        credential: ApiCredential,
        *,
        conversation_id: str,
        header_user_id: str,
        idempotency_key: str,
    ) -> tuple[dict[str, Any], bool]:
        text, prior_messages = self._validate_and_extract(payload)
        resolved = await self.resolve_identity(
            credential,
            payload=payload,
            conversation_id=conversation_id,
            header_user_id=header_user_id,
        )
        request_hash = self._request_hash(payload, resolved)
        if idempotency_key:
            if len(idempotency_key) > 255:
                raise api_error(
                    status.HTTP_400_BAD_REQUEST,
                    "The idempotency key is too long.",
                    "invalid_request_error",
                    "invalid_idempotency_key",
                    param="Idempotency-Key",
                )
            reservation, cached = await self.store.reserve_api_request(
                tenant_key=credential.tenant_key,
                principal_id=resolved.principal_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                ttl_seconds=self.settings.song_agent_api_idempotency_ttl_seconds,
            )
            if reservation == "conflict":
                raise api_error(
                    status.HTTP_409_CONFLICT,
                    "The idempotency key was already used with a different request.",
                    "conflict_error",
                    "idempotency_conflict",
                )
            if reservation == "in_progress":
                raise api_error(
                    status.HTTP_409_CONFLICT,
                    "A request with this idempotency key is still processing.",
                    "conflict_error",
                    "idempotency_in_progress",
                )
            if reservation == "replay" and cached is not None:
                return cached, True

        request_id = idempotency_key or f"api_{uuid.uuid4().hex}"
        request_context = {
            "openai_messages": prior_messages,
            "openai_generation": {
                "temperature": payload.temperature,
                "top_p": payload.top_p,
                "max_tokens": payload.max_completion_tokens or payload.max_tokens,
            },
            "api_metadata": payload.metadata,
            "delivery_channel": "feishu" if resolved.binding is not None else "api",
            "delivery_binding_id": resolved.binding.binding_id if resolved.binding else None,
        }
        user_request = UserRequest(
            identity=resolved.identity,
            text=text,
            source="api",
            chat_id=resolved.chat_id,
            thread_id="",
            message_id=request_id,
            api_metadata=payload.metadata,
            delivery_channel="feishu" if resolved.binding is not None else "api",
            delivery_binding_id=resolved.binding.binding_id if resolved.binding else None,
            context=request_context,
        )
        try:
            result = await asyncio.wait_for(
                self.dispatcher.dispatch(request=user_request),
                timeout=self.settings.song_agent_api_sync_timeout_seconds,
            )
            response = self._response(payload, result, request_id)
            if idempotency_key:
                await self.store.complete_api_request(
                    tenant_key=credential.tenant_key,
                    principal_id=resolved.principal_id,
                    idempotency_key=idempotency_key,
                    response=response,
                )
            return response, False
        except TimeoutError as exc:
            if idempotency_key:
                await self._abandon(credential, resolved, idempotency_key)
            raise api_error(
                status.HTTP_504_GATEWAY_TIMEOUT,
                "The agent exceeded its execution time budget.",
                "server_error",
                "agent_timeout",
            ) from exc
        except HTTPException:
            if idempotency_key:
                await self._abandon(credential, resolved, idempotency_key)
            raise
        except Exception as exc:
            if idempotency_key:
                await self._abandon(credential, resolved, idempotency_key)
            self.logger.exception(
                "Agent API request failed request_id=%s tenant=%s principal=%s",
                request_id,
                credential.tenant_key,
                resolved.principal_id,
            )
            raise api_error(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                "An internal server error occurred.",
                "server_error",
                "internal_error",
            ) from exc

    async def resolve_identity(
        self,
        credential: ApiCredential,
        *,
        payload: ChatCompletionRequest | None = None,
        conversation_id: str = "",
        header_user_id: str = "",
        binding_id: str = "",
    ) -> ResolvedApiIdentity:
        payload_user = payload.user if payload else None
        principal_id = (header_user_id or payload_user or credential.principal_id).strip()
        if not principal_id or len(principal_id) > 255:
            raise api_error(
                status.HTTP_400_BAD_REQUEST,
                "The API user identifier is invalid.",
                "invalid_request_error",
                "invalid_user",
                param="user",
            )
        metadata = payload.metadata if payload else {}
        requested_channel = str(metadata.get("delivery_channel") or "")
        requested_binding = binding_id or str(metadata.get("delivery_binding_id") or "")
        if requested_channel not in {"", "api", "feishu"}:
            raise api_error(
                status.HTTP_400_BAD_REQUEST,
                f"The delivery channel '{requested_channel}' is not supported.",
                "invalid_request_error",
                "unsupported_delivery_channel",
                param="metadata.delivery_channel",
            )
        if requested_channel == "feishu" or requested_binding:
            if not requested_binding:
                raise api_error(
                    status.HTTP_400_BAD_REQUEST,
                    "A Feishu delivery binding is required.",
                    "invalid_request_error",
                    "binding_required",
                    param="metadata.delivery_binding_id",
                )
            binding = await self.store.get_api_channel_binding(
                requested_binding,
                tenant_key=credential.tenant_key,
                app_id=self.settings.song_agent_api_app_id,
                principal_id=principal_id,
            )
            if not binding:
                raise api_error(
                    status.HTTP_404_NOT_FOUND,
                    "The channel binding does not exist.",
                    "invalid_request_error",
                    "binding_not_found",
                    param="metadata.delivery_binding_id",
                )
            return ResolvedApiIdentity(
                identity=FeishuIdentity(
                    tenant_key=binding.external_tenant_key,
                    app_id=binding.external_app_id,
                    open_id=binding.external_open_id,
                    user_id=binding.external_user_id,
                    union_id=binding.external_union_id,
                ),
                chat_id=binding.external_chat_id,
                principal_id=principal_id,
                binding=binding,
            )
        chat_id = (
            conversation_id.strip()
            or str(metadata.get("conversation_id") or "").strip()
            or f"api:{principal_id}"
        )
        if len(chat_id) > 255:
            raise api_error(
                status.HTTP_400_BAD_REQUEST,
                "The conversation identifier is too long.",
                "invalid_request_error",
                "invalid_conversation_id",
                param="X-Song-Conversation-Id",
            )
        return ResolvedApiIdentity(
            identity=FeishuIdentity(
                tenant_key=credential.tenant_key,
                app_id=self.settings.song_agent_api_app_id,
                open_id=principal_id,
                user_id=principal_id,
            ),
            chat_id=chat_id,
            principal_id=principal_id,
        )

    def _validate_and_extract(
        self,
        payload: ChatCompletionRequest,
    ) -> tuple[str, list[dict[str, str]]]:
        if payload.model != self.settings.song_agent_api_model_id:
            raise api_error(
                status.HTTP_404_NOT_FOUND,
                f"The model '{payload.model}' does not exist.",
                "invalid_request_error",
                "model_not_found",
                param="model",
            )
        if len(payload.messages) > self.settings.song_agent_api_max_messages:
            raise api_error(
                status.HTTP_400_BAD_REQUEST,
                "Too many messages.",
                "invalid_request_error",
                "messages_limit_exceeded",
                param="messages",
            )
        unsupported = sorted((payload.model_fields_set | set(payload.model_extra or {})) - _SUPPORTED_FIELDS)
        if "tools" in payload.model_fields_set:
            unsupported.append("tools")
        if "tool_choice" in payload.model_fields_set:
            unsupported.append("tool_choice")
        if unsupported:
            field = sorted(set(unsupported))[0]
            raise api_error(
                status.HTTP_400_BAD_REQUEST,
                f"The parameter '{field}' is not supported.",
                "invalid_request_error",
                "unsupported_parameter",
                param=field,
            )
        if payload.n != 1:
            raise api_error(
                status.HTTP_400_BAD_REQUEST,
                "The parameter 'n' only supports the value 1.",
                "invalid_request_error",
                "unsupported_parameter",
                param="n",
            )
        normalized: list[dict[str, str]] = []
        total_chars = 0
        for index, message in enumerate(payload.messages):
            if message.role == "tool":
                raise api_error(
                    status.HTTP_400_BAD_REQUEST,
                    "The message role 'tool' is not supported.",
                    "invalid_request_error",
                    "unsupported_message_role",
                    param=f"messages[{index}].role",
                )
            if message.role not in _SUPPORTED_ROLES:
                raise api_error(
                    status.HTTP_400_BAD_REQUEST,
                    f"The message role '{message.role}' is not supported.",
                    "invalid_request_error",
                    "unsupported_message_role",
                    param=f"messages[{index}].role",
                )
            if not isinstance(message.content, str):
                raise api_error(
                    status.HTTP_400_BAD_REQUEST,
                    "Only text message content is supported.",
                    "invalid_request_error",
                    "unsupported_content_type",
                    param=f"messages[{index}].content",
                )
            if len(message.content) > self.settings.song_agent_api_max_message_chars:
                raise api_error(
                    status.HTTP_400_BAD_REQUEST,
                    "A message exceeds the character limit.",
                    "invalid_request_error",
                    "message_too_long",
                    param=f"messages[{index}].content",
                )
            total_chars += len(message.content)
            normalized.append({"role": message.role, "content": message.content})
        if total_chars > self.settings.song_agent_api_max_total_chars:
            raise api_error(
                status.HTTP_400_BAD_REQUEST,
                "The messages exceed the total character limit.",
                "invalid_request_error",
                "messages_too_long",
                param="messages",
            )
        user_indexes = [index for index, item in enumerate(normalized) if item["role"] == "user"]
        if not user_indexes or not normalized[user_indexes[-1]]["content"].strip():
            raise api_error(
                status.HTTP_400_BAD_REQUEST,
                "At least one non-empty user message is required.",
                "invalid_request_error",
                "missing_user_message",
                param="messages",
            )
        last_user_index = user_indexes[-1]
        return (
            normalized[last_user_index]["content"],
            normalized[:last_user_index] + normalized[last_user_index + 1 :],
        )

    def _response(
        self,
        payload: ChatCompletionRequest,
        result: ApplicationResult,
        request_id: str,
    ) -> dict[str, Any]:
        content = render_message(result)
        if payload.stop:
            stops = [payload.stop] if isinstance(payload.stop, str) else payload.stop
            positions = [content.find(item) for item in stops if item and item in content]
            if positions:
                content = content[: min(positions)]
        prompt_chars = sum(len(str(message.content)) for message in payload.messages)
        prompt_tokens = _estimate_tokens(prompt_chars)
        completion_tokens = _estimate_tokens(len(content))
        return {
            "id": f"chatcmpl-{request_id.replace('_', '')[:40]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": self.settings.song_agent_api_model_id,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
            "song_agent": {
                "status": result.status,
                "intent": result.intent,
                "action_id": result.action_id or None,
                "authorization_url": result.authorization_url or None,
                "data": result.data,
            },
        }

    def _request_hash(
        self,
        payload: ChatCompletionRequest,
        resolved: ResolvedApiIdentity,
    ) -> str:
        canonical = json.dumps(
            {
                "payload": payload.model_dump(mode="json"),
                "principal_id": resolved.principal_id,
                "chat_id": resolved.chat_id,
                "binding_id": resolved.binding.binding_id if resolved.binding else None,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    async def _abandon(
        self,
        credential: ApiCredential,
        resolved: ResolvedApiIdentity,
        idempotency_key: str,
    ) -> None:
        await self.store.abandon_api_request(
            tenant_key=credential.tenant_key,
            principal_id=resolved.principal_id,
            idempotency_key=idempotency_key,
        )


def _estimate_tokens(chars: int) -> int:
    return max(1, (chars + 3) // 4) if chars else 0
