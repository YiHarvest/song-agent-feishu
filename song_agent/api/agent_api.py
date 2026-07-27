"""External OpenAI-compatible Agent API under /api/v1."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Path, Request, Response, status
from fastapi.responses import StreamingResponse

from ..application.openai_adapter import OpenAIAdapter
from ..domain.results import ApplicationResult
from ..models import FeishuIdentity, PendingAction
from .agent_auth import ApiCredential, api_error, require_agent_api_credential
from .agent_schemas import BindingCodeResponse, ChatCompletionRequest

router = APIRouter(prefix="/api/v1", tags=["OpenAI 兼容 Agent API"])
Credential = Annotated[ApiCredential, Depends(require_agent_api_credential)]


@router.get(
    "/models",
    summary="列出可用模型",
    description=(
        "返回当前 Agent API 对外暴露的模型列表。调用聊天接口时，请把返回的模型 `id` 填入请求体 `model`。"
    ),
    response_description="OpenAI 兼容模型列表。",
)
async def list_models(request: Request, credential: Credential) -> dict[str, Any]:
    del credential
    model_id = request.app.state.settings.song_agent_api_model_id
    return {"object": "list", "data": [_model(model_id)]}


@router.get(
    "/models/{model_id}",
    summary="查询指定模型",
    description="按模型 ID 查询模型信息；模型不存在时返回 404。",
    response_description="指定模型的 OpenAI 兼容描述。",
)
async def retrieve_model(
    model_id: Annotated[str, Path(description="模型 ID，例如 song-agent-2.1。")],
    request: Request,
    credential: Credential,
) -> dict[str, Any]:
    del credential
    configured = request.app.state.settings.song_agent_api_model_id
    if model_id != configured:
        raise api_error(
            status.HTTP_404_NOT_FOUND,
            f"The model '{model_id}' does not exist.",
            "invalid_request_error",
            "model_not_found",
            param="model",
        )
    return _model(configured)


@router.post(
    "/chat/completions",
    summary="创建聊天补全",
    description=(
        "OpenAI Chat Completions 兼容入口。请求会转换为 Song Agent `UserRequest`，"
        "复用现有 `RequestRouter`。支持普通 JSON 和 `stream=true` SSE 响应。"
        "当前只支持文本，`n` 只能为 1，不支持 tools、tool_choice 和 tool role。"
    ),
    response_description="OpenAI Chat Completion 或 SSE 数据流。",
    response_model=None,
)
async def create_chat_completion(
    payload: ChatCompletionRequest,
    request: Request,
    response: Response,
    credential: Credential,
    x_song_user_id: Annotated[
        str,
        Header(description="可选 API 用户标识，用于用户数据隔离。"),
    ] = "",
    x_song_conversation_id: Annotated[
        str,
        Header(description="可选会话标识；相同值复用同一会话上下文。"),
    ] = "",
    idempotency_key: Annotated[
        str,
        Header(
            alias="Idempotency-Key",
            description="可选幂等键。相同键和请求直接重放首次结果。",
        ),
    ] = "",
    x_idempotency_key: Annotated[
        str,
        Header(
            alias="X-Idempotency-Key",
            description="兼容幂等请求头；优先使用 Idempotency-Key。",
        ),
    ] = "",
) -> Response | dict[str, Any]:
    adapter: OpenAIAdapter = request.app.state.openai_adapter
    key = idempotency_key or x_idempotency_key
    result, replayed = await adapter.complete(
        payload,
        credential,
        conversation_id=x_song_conversation_id,
        header_user_id=x_song_user_id,
        idempotency_key=key,
    )
    response.headers["X-Request-ID"] = result["id"]
    if key:
        response.headers["Idempotency-Key"] = key
        response.headers["Idempotency-Replayed"] = str(replayed).lower()
    if not payload.stream:
        return result
    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "X-Request-ID": result["id"],
    }
    if key:
        headers["Idempotency-Key"] = key
        headers["Idempotency-Replayed"] = str(replayed).lower()
    return StreamingResponse(
        _stream_completion(
            result, include_usage=bool(payload.stream_options and payload.stream_options.include_usage)
        ),
        media_type="text/event-stream",
        headers=headers,
    )


@router.get(
    "/capabilities",
    summary="查看 API 能力",
    description=(
        "返回当前支持的消息角色、流式输出、Pending Action、飞书绑定，"
        "以及图片、文件、语音、工具调用等能力状态。"
    ),
    response_description="Agent API 能力清单。",
)
async def capabilities(request: Request, credential: Credential) -> dict[str, Any]:
    del credential
    return {
        "object": "song_agent.capabilities",
        "model": request.app.state.settings.song_agent_api_model_id,
        "input_types": ["text"],
        "features": {
            "api": {
                "text": True,
                "images": False,
                "audio": False,
                "files": False,
            },
            "feishu": {
                "text": True,
                "images": True,
                "audio": True,
                "files": True,
            },
        },
        "chat_completions": {
            "streaming": True,
            "n": [1],
            "message_roles": ["system", "developer", "user", "assistant"],
            "text": True,
            "tools": False,
            "images": False,
            "files": False,
            "audio": False,
        },
        "pending_actions": ["confirm", "cancel"],
        "channel_bindings": ["feishu"],
    }


@router.get(
    "/health",
    summary="基础健康检查",
    description="检查 Agent API 是否启用，并返回当前模型。需要 API Key。",
    response_description="基础服务状态。",
)
async def health(request: Request, credential: Credential) -> dict[str, Any]:
    del credential
    return {
        "status": "ok",
        "api_enabled": request.app.state.settings.song_agent_api_enabled,
        "model": request.app.state.settings.song_agent_api_model_id,
    }


@router.get(
    "/health/details",
    summary="详细健康检查",
    description=(
        "检查数据库、RequestRouter、Scheduler、Outbox 和 API Key 配置。"
        "仅在 SONG_AGENT_API_HEALTH_DETAILS_ENABLED=true 时开放。"
    ),
    response_description="详细组件状态。",
)
async def health_details(request: Request, credential: Credential) -> dict[str, Any]:
    del credential
    settings = request.app.state.settings
    if not settings.song_agent_api_health_details_enabled:
        raise api_error(
            status.HTTP_404_NOT_FOUND,
            "Detailed health checks are disabled.",
            "invalid_request_error",
            "health_details_disabled",
        )
    await (await request.app.state.store.db.execute("SELECT 1")).fetchone()
    return {
        "status": "ok",
        "database": "ok",
        "request_router": "ok" if request.app.state.request_router else "unavailable",
        "scheduler": "ok",
        "outbox": "ok",
        "api_key_configured": bool(settings.song_agent_api_key),
    }


@router.post(
    "/pending-actions/{action_id}/confirm",
    summary="确认待执行动作",
    description=(
        "确认当前 API 用户拥有的 Pending Action，并交给现有 Outbox 异步执行。"
        "常用于确认创建日历、任务或提醒等写操作。"
    ),
    response_description="确认后的动作状态。",
)
async def confirm_pending_action(
    action_id: Annotated[str, Path(description="聊天响应 song_agent.action_id。")],
    request: Request,
    credential: Credential,
    x_song_user_id: Annotated[
        str,
        Header(description="创建该动作时使用的 API 用户标识。"),
    ] = "",
) -> ApplicationResult:
    identity = await _pending_action_identity(
        request,
        credential,
        action_id,
        x_song_user_id,
    )
    result = await request.app.state.pending_action_service.confirm(identity, action_id)
    if result.status == "error":
        raise api_error(
            status.HTTP_409_CONFLICT,
            result.message,
            "conflict_error",
            "pending_action_conflict",
        )
    return result


@router.post(
    "/pending-actions/{action_id}/cancel",
    summary="取消待执行动作",
    description="取消当前 API 用户拥有且仍可取消的 Pending Action。",
    response_description="取消后的动作状态。",
)
async def cancel_pending_action(
    action_id: Annotated[str, Path(description="聊天响应 song_agent.action_id。")],
    request: Request,
    credential: Credential,
    x_song_user_id: Annotated[
        str,
        Header(description="创建该动作时使用的 API 用户标识。"),
    ] = "",
) -> ApplicationResult:
    identity = await _pending_action_identity(
        request,
        credential,
        action_id,
        x_song_user_id,
    )
    result = await request.app.state.pending_action_service.cancel(identity, action_id)
    if result.status == "error":
        raise api_error(
            status.HTTP_409_CONFLICT,
            result.message,
            "conflict_error",
            "pending_action_conflict",
        )
    return result


@router.post(
    "/channel-bindings/feishu/code",
    summary="生成飞书绑定码",
    description=(
        "为当前 API 用户生成一次性绑定码。将响应中的 `command` 原样发送给飞书机器人，即可绑定飞书身份和会话。"
    ),
    response_description="一次性绑定码、飞书命令和过期时间。",
)
async def create_feishu_binding_code(
    request: Request,
    credential: Credential,
    x_song_user_id: Annotated[
        str,
        Header(description="需要绑定飞书渠道的 API 用户标识。"),
    ] = "",
) -> BindingCodeResponse:
    principal_id = (x_song_user_id or credential.principal_id).strip()
    if not principal_id or len(principal_id) > 255:
        raise api_error(
            status.HTTP_400_BAD_REQUEST,
            "The API user identifier is invalid.",
            "invalid_request_error",
            "invalid_user",
            param="X-Song-User-Id",
        )
    code, expires_at = await request.app.state.api_access_service.create_binding_code(
        tenant_key=credential.tenant_key,
        principal_id=principal_id,
    )
    return BindingCodeResponse(
        code=code,
        command=f"绑定 {code}",
        expires_at=expires_at,
    )


@router.get(
    "/channel-bindings",
    summary="列出渠道绑定",
    description="列出当前 API 用户拥有的全部飞书渠道绑定。",
    response_description="渠道绑定列表。",
)
async def list_channel_bindings(
    request: Request,
    credential: Credential,
    x_song_user_id: Annotated[
        str,
        Header(description="要查询绑定的 API 用户标识。"),
    ] = "",
) -> dict[str, Any]:
    principal_id = (x_song_user_id or credential.principal_id).strip()
    bindings = await request.app.state.store.list_api_channel_bindings(
        tenant_key=credential.tenant_key,
        app_id=request.app.state.settings.song_agent_api_app_id,
        principal_id=principal_id,
    )
    return {
        "object": "list",
        "data": [_public_binding(binding) for binding in bindings],
    }


@router.delete(
    "/channel-bindings/{binding_id}",
    summary="删除渠道绑定",
    description="删除当前 API 用户拥有的指定飞书渠道绑定。",
    response_description="删除成功时无响应正文。",
    status_code=204,
)
async def delete_channel_binding(
    binding_id: Annotated[str, Path(description="渠道绑定 ID，例如 binding_xxx。")],
    request: Request,
    credential: Credential,
    x_song_user_id: Annotated[
        str,
        Header(description="拥有该绑定的 API 用户标识。"),
    ] = "",
) -> Response:
    principal_id = (x_song_user_id or credential.principal_id).strip()
    deleted = await request.app.state.store.delete_api_channel_binding(
        binding_id,
        tenant_key=credential.tenant_key,
        app_id=request.app.state.settings.song_agent_api_app_id,
        principal_id=principal_id,
    )
    if not deleted:
        raise api_error(
            status.HTTP_404_NOT_FOUND,
            "The channel binding does not exist.",
            "invalid_request_error",
            "binding_not_found",
        )
    return Response(status_code=204)


def _model(model_id: str) -> dict[str, Any]:
    return {
        "id": model_id,
        "object": "model",
        "created": 0,
        "owned_by": "song-agent",
    }


async def _stream_completion(
    response: dict[str, Any],
    *,
    include_usage: bool,
) -> AsyncIterator[str]:
    choice = response["choices"][0]
    base = {
        "id": response["id"],
        "object": "chat.completion.chunk",
        "created": response["created"],
        "model": response["model"],
    }
    first = {
        **base,
        "choices": [
            {
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "content": choice["message"]["content"],
                },
                "finish_reason": None,
            }
        ],
    }
    yield f"data: {json.dumps(first, ensure_ascii=False)}\n\n"
    final = {
        **base,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "song_agent": response.get("song_agent", {}),
    }
    yield f"data: {json.dumps(final, ensure_ascii=False)}\n\n"
    if include_usage:
        usage = {**base, "choices": [], "usage": response["usage"]}
        yield f"data: {json.dumps(usage, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


async def _pending_action_identity(
    request: Request,
    credential: ApiCredential,
    action_id: str,
    header_user_id: str,
) -> FeishuIdentity:
    principal_id = (header_user_id or credential.principal_id).strip()
    action: PendingAction | None = await request.app.state.store.get_pending_action(action_id)
    if not action:
        raise api_error(
            status.HTTP_404_NOT_FOUND,
            "The pending action does not exist.",
            "invalid_request_error",
            "pending_action_not_found",
        )
    settings = request.app.state.settings
    if (
        action.tenant_key == credential.tenant_key
        and action.app_id == settings.song_agent_api_app_id
        and action.creator_subject_id == principal_id
    ):
        return FeishuIdentity(
            tenant_key=action.tenant_key,
            app_id=action.app_id,
            open_id=action.creator_open_id,
            user_id=action.creator_subject_id,
        )
    bindings = await request.app.state.store.list_api_channel_bindings(
        tenant_key=credential.tenant_key,
        app_id=settings.song_agent_api_app_id,
        principal_id=principal_id,
    )
    for binding in bindings:
        if (
            action.tenant_key == binding.external_tenant_key
            and action.app_id == binding.external_app_id
            and action.creator_subject_id == binding.external_subject_id
            and action.creator_open_id == binding.external_open_id
        ):
            return FeishuIdentity(
                tenant_key=binding.external_tenant_key,
                app_id=binding.external_app_id,
                open_id=binding.external_open_id,
                user_id=binding.external_user_id,
                union_id=binding.external_union_id,
            )
    raise api_error(
        status.HTTP_404_NOT_FOUND,
        "The pending action does not exist.",
        "invalid_request_error",
        "pending_action_not_found",
    )


def _public_binding(binding: Any) -> dict[str, Any]:
    return {
        "id": binding.binding_id,
        "object": "channel_binding",
        "provider": binding.provider,
        "external_subject_id": binding.external_subject_id,
        "external_chat_id": binding.external_chat_id,
        "created_at": binding.created_at,
    }
