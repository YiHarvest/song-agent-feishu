from __future__ import annotations

import json
import time
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from ..bootstrap.v1 import ApplicationRuntime
from ..infrastructure.database import SCHEMA_REVISION
from ..kernel.errors import ProviderError
from ..kernel.models import (
    ConversationRef,
    DeliveryTarget,
    IntentExecutionPayload,
    NaturalLanguagePayload,
    PrincipalIdentity,
    RequestEnvelope,
    TextPart,
)


class IntentExecuteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    arguments: dict[str, Any] = Field(default_factory=dict)
    delivery_binding_id: str = ""


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")
    role: str
    content: str | list[dict[str, Any]]


class ChatCompletionBody(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    user: str = ""


def api_router() -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        runtime = _runtime(request)
        return {
            "status": "ok",
            "schema_revision": SCHEMA_REVISION,
            "modules": [
                {
                    "module_id": module.manifest.module_id,
                    "version": module.manifest.version,
                }
                for module in runtime.modules.modules
            ],
            "intents": len(runtime.catalog),
            "providers": runtime.modules.providers.describe(),
        }

    @router.get("/v1/models")
    async def models(
        request: Request,
        principal: Annotated[PrincipalIdentity, Depends(_principal)],
    ) -> dict[str, Any]:
        runtime = _runtime(request)
        return {
            "object": "list",
            "data": [
                {
                    "id": runtime.settings.api.model_id,
                    "object": "model",
                    "created": 0,
                    "owned_by": "song-agent",
                }
            ],
        }

    @router.post("/v1/chat/completions")
    async def chat_completions(
        body: ChatCompletionBody,
        request: Request,
        principal: Annotated[PrincipalIdentity, Depends(_principal)],
        x_request_id: Annotated[str | None, Header()] = None,
        x_conversation_id: Annotated[str | None, Header()] = None,
    ) -> Any:
        runtime = _runtime(request)
        if body.model != runtime.settings.api.model_id:
            raise HTTPException(status_code=404, detail="model_not_found")
        if len(body.messages) > runtime.settings.api.max_messages:
            raise HTTPException(status_code=422, detail="too_many_messages")
        if sum(_message_chars(message) for message in body.messages) > (
            runtime.settings.api.max_total_chars
        ):
            raise HTTPException(status_code=422, detail="messages_too_large")
        text = _last_user_text(body.messages)
        if not text:
            raise HTTPException(status_code=422, detail="a non-empty user text message is required")
        conversation_id = x_conversation_id or body.user or principal.principal_id
        envelope = RequestEnvelope(
            request_id=x_request_id or f"req_{uuid.uuid4().hex}",
            principal=principal,
            delivery_target=_api_target(runtime, principal),
            conversation=ConversationRef(
                tenant_id=principal.tenant_id,
                channel="openai_api",
                channel_account_id=runtime.settings.api.key_name,
                conversation_id=conversation_id,
                principal_id=principal.principal_id,
            ),
            source="openai_api",
            payload=NaturalLanguagePayload(content=(TextPart(text=text),)),
        )
        async with runtime.coordinator.lock_for(envelope):
            routed = await runtime.router.route(envelope)
            result = routed.result
            if routed.envelope is not None:
                result = await runtime.dispatcher.execute(routed.envelope)
        if result is None:
            raise RuntimeError("router returned neither envelope nor result")
        response = _openai_response(runtime, result.message or _result_text(result))
        if body.stream:
            return StreamingResponse(
                _stream_response(response), media_type="text/event-stream"
            )
        return response

    @router.post("/api/v1/intents/{intent_id}/execute")
    async def execute_intent(
        intent_id: str,
        body: IntentExecuteBody,
        request: Request,
        principal: Annotated[PrincipalIdentity, Depends(_principal)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        x_request_id: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        runtime = _runtime(request)
        target = _api_target(runtime, principal)
        if body.delivery_binding_id:
            resolved = await runtime.delivery_bindings.resolve(
                body.delivery_binding_id, principal=principal
            )
            if resolved is None:
                raise HTTPException(status_code=404, detail="delivery_binding_not_found")
            target = resolved
        envelope = RequestEnvelope(
            request_id=x_request_id or f"req_{uuid.uuid4().hex}",
            principal=principal,
            delivery_target=target,
            source="intent_api",
            payload=IntentExecutionPayload(intent_id=intent_id, arguments=body.arguments),
            metadata={"idempotency_key": idempotency_key or ""},
        )
        result = await runtime.dispatcher.execute(envelope)
        return result.model_dump(mode="json")

    @router.get("/api/v1/actions/{action_id}")
    async def get_action(
        action_id: str,
        request: Request,
        principal: Annotated[PrincipalIdentity, Depends(_principal)],
    ) -> dict[str, Any]:
        action = await _runtime(request).actions.get(action_id, principal)
        if action is None:
            raise HTTPException(status_code=404, detail="action_not_found")
        return {
            "action_id": action.action_id,
            "intent_id": action.intent_id,
            "arguments": action.arguments,
            "status": action.status.value,
            "risk_level": action.risk_level.value,
            "expires_at": action.expires_at.isoformat(),
            "result": action.result.model_dump(mode="json") if action.result else None,
        }

    @router.post("/api/v1/actions/{action_id}/confirm")
    async def confirm_action(
        action_id: str,
        request: Request,
        principal: Annotated[PrincipalIdentity, Depends(_principal)],
    ) -> dict[str, Any]:
        return (await _runtime(request).actions.confirm(action_id, principal)).model_dump(
            mode="json"
        )

    @router.post("/api/v1/actions/{action_id}/cancel")
    async def cancel_action(
        action_id: str,
        request: Request,
        principal: Annotated[PrincipalIdentity, Depends(_principal)],
    ) -> dict[str, Any]:
        return (await _runtime(request).actions.cancel(action_id, principal)).model_dump(
            mode="json"
        )

    @router.post("/api/v1/actions/{action_id}/reconcile")
    async def reconcile_action(
        action_id: str,
        request: Request,
        principal: Annotated[PrincipalIdentity, Depends(_principal)],
    ) -> dict[str, Any]:
        return (await _runtime(request).actions.reconcile(action_id, principal)).model_dump(
            mode="json"
        )

    @router.post("/api/v1/actions/{action_id}/retry")
    async def retry_action(
        action_id: str,
        request: Request,
        principal: Annotated[PrincipalIdentity, Depends(_principal)],
    ) -> dict[str, Any]:
        return (await _runtime(request).actions.retry_unknown(action_id, principal)).model_dump(
            mode="json"
        )

    @router.post("/adapters/feishu/events", include_in_schema=False)
    async def feishu_events(request: Request) -> dict[str, Any]:
        adapter = _runtime(request).feishu_channel
        if adapter is None:
            raise HTTPException(status_code=404, detail="feishu_channel_disabled")
        raw_body = await request.body()
        try:
            payload = json.loads(raw_body)
            if not isinstance(payload, dict):
                raise ValueError("event body must be an object")
            return await adapter.handle(
                payload,
                raw_body=raw_body,
                headers={key.lower(): value for key, value in request.headers.items()},
            )
        except PermissionError as error:
            raise HTTPException(status_code=401, detail=str(error)) from error
        except (ValueError, json.JSONDecodeError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @router.get("/adapters/feishu/oauth/callback", include_in_schema=False)
    async def feishu_oauth_callback(
        request: Request,
        code: str = "",
        state: str = "",
    ) -> HTMLResponse:
        oauth = _runtime(request).feishu_oauth
        if oauth is None:
            raise HTTPException(status_code=404, detail="feishu_oauth_disabled")
        if not code or not state:
            raise HTTPException(status_code=400, detail="invalid_oauth_callback")
        try:
            await oauth.callback(code=code, state=state)
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except ProviderError as error:
            raise HTTPException(status_code=502, detail=error.error_code) from error
        return HTMLResponse(
            "<!doctype html><meta charset='utf-8'><title>授权成功</title>"
            "<p>飞书授权成功，可以关闭此页面并返回会话。</p>"
        )

    return router


async def _principal(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> PrincipalIdentity:
    scheme, _, credential = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not credential:
        raise HTTPException(status_code=401, detail="missing_bearer_token")
    principal = _runtime(request).authenticator.authenticate(credential)
    if principal is None:
        raise HTTPException(status_code=401, detail="invalid_api_key")
    rate_key = f"{principal.tenant_id}:{principal.principal_id}"
    if not await _runtime(request).rate_limiter.allow(rate_key):
        raise HTTPException(status_code=429, detail="rate_limit_exceeded")
    return principal


def _runtime(request: Request) -> ApplicationRuntime:
    return request.app.state.runtime


def _api_target(runtime: ApplicationRuntime, principal: PrincipalIdentity) -> DeliveryTarget:
    return DeliveryTarget(
        channel="openai_api",
        channel_account_id=runtime.settings.api.key_name,
        destination_id=principal.principal_id,
    )


def _last_user_text(messages: list[ChatMessage]) -> str:
    for message in reversed(messages):
        if message.role != "user":
            continue
        if isinstance(message.content, str):
            return message.content.strip()
        values = [
            str(part.get("text") or "")
            for part in message.content
            if part.get("type") in {"text", "input_text"}
        ]
        return "\n".join(value for value in values if value).strip()
    return ""


def _message_chars(message: ChatMessage) -> int:
    if isinstance(message.content, str):
        return len(message.content)
    return sum(len(str(part.get("text") or "")) for part in message.content)


def _result_text(result: Any) -> str:
    return json.dumps(result.data, ensure_ascii=False, default=str)


def _openai_response(runtime: ApplicationRuntime, text: str) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": runtime.settings.api.model_id,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


async def _stream_response(response: dict[str, Any]):
    choice = response["choices"][0]
    chunks = (
        {"role": "assistant", "content": ""},
        {"content": choice["message"]["content"]},
    )
    for delta in chunks:
        payload = {
            "id": response["id"],
            "object": "chat.completion.chunk",
            "created": response["created"],
            "model": response["model"],
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
        }
        yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    final = {
        "id": response["id"],
        "object": "chat.completion.chunk",
        "created": response["created"],
        "model": response["model"],
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(final, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"
