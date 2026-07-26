"""
飞书 OAuth 授权模块。

实现飞书用户身份 OAuth 2.0 授权流程，支持令牌获取、刷新和回调处理。
授权成功后自动跳转回飞书聊天窗口。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from ..config import Settings
from ..models import FeishuIdentity, OAuthToken, UserTokenContext
from ..store import SqliteStore


class FeishuOAuth:
    """
    飞书 OAuth 授权管理器。

    处理用户身份授权流程，包括创建授权链接、处理回调、令牌刷新等。
    """

    def __init__(self, settings: Settings, store: SqliteStore) -> None:
        self.settings = settings
        self.store = store
        self.logger = logging.getLogger(__name__)
        self._refresh_locks: dict[str, asyncio.Lock] = {}
        self._worker_id = f"oauth:{uuid.uuid4()}"
        self.on_authorized: (
            Callable[[FeishuIdentity, str, str], Awaitable[None]] | None
        ) = None
        self.router = APIRouter()
        self.router.add_api_route("/oauth/callback", self.handle_callback, methods=["GET"])

    async def create_authorization_url(
        self,
        identity: FeishuIdentity,
        chat_id: str,
        required_scopes: tuple[str, ...] | None = None,
        original_request: str = "",
    ) -> str:
        state = secrets.token_urlsafe(32)
        await self.store.save_oauth_authorization(
            state,
            identity,
            chat_id,
            int(time.time()) + 600,
            original_request=original_request,
        )
        scopes = tuple(
            dict.fromkeys(("offline_access", *(required_scopes or self.settings.required_oauth_scopes)))
        )
        query = urlencode(
            {
                "client_id": self.settings.feishu_app_id,
                "redirect_uri": f"{self.settings.base_url}/oauth/callback",
                "scope": " ".join(scopes),
                "state": state,
            }
        )
        return f"{self.settings.domain}/open-apis/authen/v1/authorize?{query}"

    async def get_valid_token_context(
        self,
        identity: FeishuIdentity,
        required_scopes: tuple[str, ...] | None = None,
    ) -> UserTokenContext | None:
        token = await self.store.get_token(
            identity.subject_id,
            tenant_key=identity.tenant_key,
            app_id=identity.app_id,
        )
        if not token:
            self.logger.warning(
                "未找到飞书用户令牌 subject=%s tenant=%s app=%s required_scopes=%s",
                identity.subject_id,
                identity.tenant_key,
                identity.app_id,
                required_scopes or self.settings.required_oauth_scopes,
            )
            return None
        scopes = {item for item in token.scope.replace(",", " ").split() if item}
        missing_scopes = [
            item
            for item in (required_scopes or self.settings.required_oauth_scopes)
            if item not in scopes
        ]
        if missing_scopes:
            self.logger.warning(
                "飞书用户令牌 scope 不足 subject=%s missing_scopes=%s granted_scopes=%s",
                identity.subject_id,
                missing_scopes,
                sorted(scopes),
            )
            return None
        now = int(time.time() * 1000)
        if token.expires_at > now + 60_000:
            self.logger.info(
                "飞书用户令牌可用 subject=%s expires_in_seconds=%d scopes=%s",
                identity.subject_id,
                (token.expires_at - now) // 1000,
                sorted(scopes),
            )
            return self._context(token)
        if token.refresh_expires_at <= now + 60_000:
            self.logger.warning(
                "飞书用户令牌及刷新令牌均已过期 subject=%s "
                "access_expires_in_seconds=%d refresh_expires_in_seconds=%d",
                identity.subject_id,
                (token.expires_at - now) // 1000,
                (token.refresh_expires_at - now) // 1000,
            )
            return None
        self.logger.info(
            "飞书用户令牌即将过期，开始刷新 subject=%s expires_in_seconds=%d",
            identity.subject_id,
            (token.expires_at - now) // 1000,
        )
        lock_key = ":".join((identity.tenant_key, identity.app_id, identity.subject_id))
        lock = self._refresh_locks.setdefault(lock_key, asyncio.Lock())
        try:
            async with lock:
                # Another request may have refreshed while this request was waiting.
                token = await self.store.get_token(
                    identity.subject_id,
                    tenant_key=identity.tenant_key,
                    app_id=identity.app_id,
                )
                if not token:
                    return None
                now = int(time.time() * 1000)
                if token.expires_at > now + 60_000:
                    return self._context(token)
                lease_owner = f"{self._worker_id}:{uuid.uuid4()}"
                claimed = await self.store.claim_token_refresh(
                    identity.subject_id,
                    tenant_key=identity.tenant_key,
                    app_id=identity.app_id,
                    owner_id=lease_owner,
                )
                if claimed is None:
                    return await self._wait_for_concurrent_refresh(identity)
                try:
                    payload = await self._exchange_token(
                        {
                            "grant_type": "refresh_token",
                            "refresh_token": claimed.refresh_token,
                        }
                    )
                    next_token = self._stored_token(identity, payload)
                    if not next_token.refresh_token:
                        next_token.refresh_token = claimed.refresh_token
                        next_token.refresh_expires_at = claimed.refresh_expires_at
                    saved = await self.store.save_refreshed_token(
                        next_token,
                        owner_id=lease_owner,
                    )
                    if not saved:
                        return await self._wait_for_concurrent_refresh(identity)
                    return self._context(next_token)
                except Exception:
                    await self.store.fail_token_refresh(
                        identity.subject_id,
                        tenant_key=identity.tenant_key,
                        app_id=identity.app_id,
                        owner_id=lease_owner,
                    )
                    self.logger.exception("刷新飞书用户令牌失败")
                    return None
        finally:
            if not lock.locked():
                self._refresh_locks.pop(lock_key, None)

    async def _wait_for_concurrent_refresh(
        self,
        identity: FeishuIdentity,
    ) -> UserTokenContext | None:
        for _ in range(50):
            await asyncio.sleep(0.1)
            token = await self.store.get_token(
                identity.subject_id,
                tenant_key=identity.tenant_key,
                app_id=identity.app_id,
            )
            if token and token.expires_at > int(time.time() * 1000) + 60_000:
                return self._context(token)
        return None

    async def handle_callback(self, code: str = "", state: str = "") -> HTMLResponse:
        if not code or not state:
            raise HTTPException(400, "授权链接无效或已过期，请回到飞书重新发起授权。")
        pending = await self.store.consume_oauth_authorization(state)
        if not pending:
            raise HTTPException(400, "授权链接无效或已过期，请回到飞书重新发起授权。")
        identity, chat_id, original_request = pending
        try:
            payload = await self._exchange_token(
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": f"{self.settings.base_url}/oauth/callback",
                }
            )
            actual_user = await self._current_user(payload["access_token"])
            if actual_user.get("open_id") != identity.open_id:
                raise RuntimeError("授权账号与消息发送者不一致")
            await self.store.save_token(self._stored_token(identity, payload, actual_user))
            if self.on_authorized:
                await self.on_authorized(identity, chat_id, original_request)
            # 授权成功后自动跳转回飞书聊天
            feishu_link = f"https://applink.feishu.cn/client/chat/open?openChatId={chat_id}"
            return HTMLResponse(
                f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>授权成功</title>
    <meta http-equiv="refresh" content="2;url='{feishu_link}'">
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
               display: flex; justify-content: center; align-items: center; height: 100vh;
               margin: 0; background: #f5f5f5; }}
        .container {{ text-align: center; padding: 40px; background: white; border-radius: 12px;
                      box-shadow: 0 2px 12px rgba(0,0,0,0.1); }}
        .icon {{ font-size: 48px; margin-bottom: 16px; }}
        h1 {{ color: #1e88e5; margin: 0 0 12px; font-size: 24px; }}
        p {{ color: #666; margin: 0; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="icon">✅</div>
        <h1>授权成功</h1>
        <p>正在返回飞书...</p>
        <p><a href="{feishu_link}">点击这里手动跳转</a></p>
    </div>
    <script>setTimeout(function() {{ window.location.href = "{feishu_link}"; }}, 1500);</script>
</body>
</html>
""",
                status_code=200,
            )
        except Exception as error:
            self.logger.exception("处理飞书 OAuth 回调失败")
            raise HTTPException(500, f"授权失败：{error}") from error

    async def _exchange_token(self, data: dict[str, str]) -> dict:
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            response = await client.post(
                f"{self.settings.domain}/open-apis/authen/v2/oauth/token",
                json={
                    "client_id": self.settings.feishu_app_id,
                    "client_secret": self.settings.feishu_app_secret,
                    **data,
                },
            )
        payload = response.json()
        if response.is_error or payload.get("code") or not payload.get("access_token"):
            raise RuntimeError(payload.get("msg") or f"令牌接口返回 HTTP {response.status_code}")
        return payload

    async def _current_user(self, access_token: str) -> dict[str, str]:
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            response = await client.get(
                f"{self.settings.domain}/open-apis/authen/v1/user_info",
                headers={"Authorization": f"Bearer {access_token}"},
            )
        payload = response.json()
        data = payload.get("data", {})
        open_id = data.get("open_id")
        if response.is_error or payload.get("code") or not open_id:
            raise RuntimeError(payload.get("msg") or "无法获取授权用户信息")
        return {
            "open_id": open_id,
            "user_id": data.get("user_id", ""),
            "union_id": data.get("union_id", ""),
        }

    @staticmethod
    def _stored_token(
        identity: FeishuIdentity,
        payload: dict,
        actual_user: dict[str, str] | None = None,
    ) -> OAuthToken:
        now = int(time.time() * 1000)
        actual_user = actual_user or {}
        return OAuthToken(
            user_id=identity.subject_id,
            tenant_key=identity.tenant_key,
            app_id=identity.app_id,
            open_id=actual_user.get("open_id") or identity.open_id,
            tenant_user_id=actual_user.get("user_id") or identity.user_id,
            union_id=actual_user.get("union_id") or identity.union_id,
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token", ""),
            expires_at=now + int(payload.get("expires_in", 7200)) * 1000,
            refresh_expires_at=now + int(payload.get("refresh_token_expires_in", 30 * 86400)) * 1000,
            scope=payload.get("scope", ""),
        )

    @staticmethod
    def _context(token: OAuthToken) -> UserTokenContext:
        scopes = frozenset(item for item in token.scope.replace(",", " ").split() if item)
        return UserTokenContext(
            tenant_key=token.tenant_key,
            app_id=token.app_id,
            subject_id=token.user_id,
            open_id=token.open_id or token.user_id,
            access_token=token.access_token,
            expires_at=datetime.fromtimestamp(token.expires_at / 1000, UTC),
            scopes=scopes,
        )
