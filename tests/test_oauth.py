from urllib.parse import parse_qs, urlparse

import pytest

from song_agent.config import Settings
from song_agent.feishu.oauth import FeishuOAuth
from song_agent.models import FeishuIdentity


class AuthorizationStore:
    def __init__(self) -> None:
        self.saved: tuple[str, FeishuIdentity, str, int, str] | None = None

    async def save_oauth_authorization(
        self,
        state: str,
        identity: FeishuIdentity,
        chat_id: str,
        expires_at: int,
        original_request: str = "",
    ) -> None:
        self.saved = (state, identity, chat_id, expires_at, original_request)


def make_settings() -> Settings:
    return Settings(
        feishu_app_id="cli-test",
        feishu_app_secret="secret",
        public_base_url="https://agent.example",
        llm_base_url="https://llm.example/v1",
        llm_api_key="key",
        llm_model="model",
    )


@pytest.mark.asyncio
async def test_authorization_url_requests_only_operation_scopes() -> None:
    store = AuthorizationStore()
    oauth = FeishuOAuth(make_settings(), store)  # type: ignore[arg-type]
    identity = FeishuIdentity(app_id="cli-test", open_id="open-id")

    url = await oauth.create_authorization_url(
        identity,
        "chat-id",
        ("docx:document", "docx:document"),
    )

    query = parse_qs(urlparse(url).query)
    assert query["scope"] == ["offline_access docx:document"]
    assert query["redirect_uri"] == ["https://agent.example/oauth/callback"]
    assert store.saved is not None
    # 验证保存了正确的参数（包括空的original_request）
    assert store.saved[1] == identity
    assert store.saved[2] == "chat-id"
    assert store.saved[4] == ""  # 默认为空字符串
    assert store.saved[1:3] == (identity, "chat-id")
