"""Authentication and process-local rate limiting for the external Agent API."""

from __future__ import annotations

import asyncio
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..config import Settings

_bearer_scheme = HTTPBearer(
    auto_error=False,
    description=(
        "Song Agent API Key。点击 Swagger 右上角 Authorize，"
        "只填写 SONG_AGENT_API_KEY 的值，不要手动添加 Bearer 前缀。"
    ),
)


def api_error(
    status_code: int,
    message: str,
    error_type: str,
    code: str,
    *,
    param: str | None = None,
    headers: dict[str, str] | None = None,
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={
            "message": message,
            "type": error_type,
            "param": param,
            "code": code,
        },
        headers=headers,
    )


@dataclass(frozen=True, slots=True)
class ApiCredential:
    key_name: str
    tenant_key: str
    principal_id: str


class ApiRateLimiter:
    def __init__(self) -> None:
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def acquire(self, key: str, limit: int) -> tuple[bool, int]:
        now = time.monotonic()
        cutoff = now - 60
        async with self._lock:
            timestamps = self._requests[key]
            while timestamps and timestamps[0] <= cutoff:
                timestamps.popleft()
            if len(timestamps) >= limit:
                retry_after = max(1, int(60 - (now - timestamps[0])) + 1)
                return False, retry_after
            timestamps.append(now)
            return True, 0


async def require_agent_api_credential(
    request: Request,
    authorization: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(_bearer_scheme),
    ],
) -> ApiCredential:
    settings: Settings = request.app.state.settings
    if not settings.song_agent_api_enabled:
        raise api_error(
            status.HTTP_404_NOT_FOUND,
            "The Agent API is disabled.",
            "invalid_request_error",
            "api_disabled",
        )
    configured = settings.song_agent_api_key
    if not settings.agent_api_configured or configured is None:
        raise api_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The Agent API is not configured.",
            "server_error",
            "api_not_configured",
        )
    scheme = authorization.scheme if authorization else ""
    supplied = authorization.credentials if authorization else ""
    if (
        scheme.lower() != "bearer"
        or not supplied
        or not secrets.compare_digest(configured.get_secret_value(), supplied)
    ):
        raise api_error(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid API key.",
            "authentication_error",
            "invalid_api_key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    credential = ApiCredential(
        key_name=settings.song_agent_api_key_name,
        tenant_key=settings.song_agent_api_default_tenant,
        principal_id=settings.song_agent_api_key_name,
    )
    limiter: ApiRateLimiter = request.app.state.agent_api_rate_limiter
    allowed, retry_after = await limiter.acquire(
        f"{credential.tenant_key}:{credential.key_name}",
        settings.song_agent_api_rate_limit_per_minute,
    )
    if not allowed:
        raise api_error(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Rate limit exceeded.",
            "rate_limit_error",
            "rate_limit_exceeded",
            headers={"Retry-After": str(retry_after)},
        )
    return credential
