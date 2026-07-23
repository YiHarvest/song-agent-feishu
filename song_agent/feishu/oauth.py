"""
飞书 OAuth 授权模块。

实现飞书用户身份 OAuth 2.0 授权流程，支持令牌获取、刷新和回调处理。
授权成功后自动跳转回飞书聊天窗口。
"""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from ..config import Settings
from ..models import OAuthToken
from ..store import JsonStore


@dataclass
class PendingAuthorization:
    user_id: str
    chat_id: str
    expires_at: int


class FeishuOAuth:
    """
    飞书 OAuth 授权管理器。

    处理用户身份授权流程，包括创建授权链接、处理回调、令牌刷新等。
    """

    def __init__(self, settings: Settings, store: JsonStore) -> None:
        self.settings = settings
        self.store = store
        self.logger = logging.getLogger(__name__)
        self.pending: dict[str, PendingAuthorization] = {}
        self.on_authorized: Callable[[str], Awaitable[None]] | None = None
        self.router = APIRouter()
        self.router.add_api_route("/oauth/callback", self.handle_callback, methods=["GET"])

    def create_authorization_url(self, user_id: str, chat_id: str) -> str:
        state = secrets.token_urlsafe(32)
        self.pending[state] = PendingAuthorization(user_id, chat_id, int(time.time()) + 600)
        query = urlencode(
            {
                "client_id": self.settings.feishu_app_id,
                "redirect_uri": f"{self.settings.base_url}/oauth/callback",
                "scope": " ".join(("offline_access", *self.settings.required_oauth_scopes)),
                "state": state,
            }
        )
        return f"{self.settings.domain}/open-apis/authen/v1/authorize?{query}"

    async def get_valid_access_token(
        self, user_id: str, required_scopes: tuple[str, ...] | None = None
    ) -> str | None:
        token = self.store.get_token(user_id)
        if not token:
            return None
        scopes = {item for item in token.scope.replace(",", " ").split() if item}
        if any(item not in scopes for item in (required_scopes or self.settings.required_oauth_scopes)):
            return None
        now = int(time.time() * 1000)
        if token.expires_at > now + 60_000:
            return token.access_token
        if token.refresh_expires_at <= now + 60_000:
            return None
        try:
            payload = await self._exchange_token(
                {"grant_type": "refresh_token", "refresh_token": token.refresh_token}
            )
            next_token = self._stored_token(user_id, payload)
            await self.store.save_token(next_token)
            return next_token.access_token
        except Exception:
            self.logger.exception("刷新飞书用户令牌失败")
            return None

    async def handle_callback(self, code: str = "", state: str = "") -> HTMLResponse:
        pending = self.pending.pop(state, None)
        if not code or not pending or pending.expires_at < int(time.time()):
            raise HTTPException(400, "授权链接无效或已过期，请回到飞书重新发起授权。")
        try:
            payload = await self._exchange_token(
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": f"{self.settings.base_url}/oauth/callback",
                }
            )
            actual_user = await self._current_user(payload["access_token"])
            if actual_user != pending.user_id:
                raise RuntimeError("授权账号与消息发送者不一致")
            await self.store.save_token(self._stored_token(pending.user_id, payload))
            if self.on_authorized:
                await self.on_authorized(pending.chat_id)
            # 授权成功后自动跳转回飞书聊天
            feishu_link = f"https://applink.feishu.cn/client/chat/open?openChatId={pending.chat_id}"
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
        async with httpx.AsyncClient(timeout=20) as client:
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

    async def _current_user(self, access_token: str) -> str:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                f"{self.settings.domain}/open-apis/authen/v1/user_info",
                headers={"Authorization": f"Bearer {access_token}"},
            )
        payload = response.json()
        user_id = payload.get("data", {}).get("open_id")
        if response.is_error or payload.get("code") or not user_id:
            raise RuntimeError(payload.get("msg") or "无法获取授权用户信息")
        return user_id

    @staticmethod
    def _stored_token(user_id: str, payload: dict) -> OAuthToken:
        now = int(time.time() * 1000)
        return OAuthToken(
            user_id=user_id,
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token", ""),
            expires_at=now + int(payload.get("expires_in", 7200)) * 1000,
            refresh_expires_at=now + int(payload.get("refresh_token_expires_in", 30 * 86400)) * 1000,
            scope=payload.get("scope", ""),
        )
