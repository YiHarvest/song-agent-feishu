"""OAuth 恢复请求模型（Q10）。

`original_request` 列从纯文本升级为版本化 JSON；数据库列类型不变（TEXT）。
不保存 token / 附件二进制 / 解析全文 / 文档正文。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from ...domain.intents import UserRequest
from ...models import FeishuIdentity


class OAuthResumeRequest(BaseModel):
    version: int = 1

    source: str = "feishu"
    text: str = ""

    tenant_key: str = ""
    app_id: str = ""
    principal_id: str = ""
    open_id: str | None = None
    user_id: str | None = None
    union_id: str | None = None

    chat_id: str = ""
    chat_type: str = "p2p"
    thread_id: str = ""
    root_id: str = ""

    original_message_id: str = ""
    original_event_id: str = ""

    attachment_ids: list[str] = Field(default_factory=list)
    retrieved_result_refs: list[str] = Field(default_factory=list)

    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    timezone: str = "Asia/Shanghai"

    def to_identity(self) -> FeishuIdentity:
        return FeishuIdentity(
            tenant_key=self.tenant_key,
            app_id=self.app_id,
            open_id=self.open_id or self.principal_id,
            user_id=self.user_id or "",
            union_id=self.union_id or "",
        )

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, raw: str) -> OAuthResumeRequest:
        return cls.model_validate(json.loads(raw))

    @classmethod
    def from_legacy_text(cls, text: str) -> OAuthResumeRequest:
        """一次性兼容：旧纯文本 original_request 只能恢复最小请求。"""
        return cls(text=text)

    @classmethod
    def from_request(cls, request: UserRequest) -> OAuthResumeRequest:
        identity = request.identity
        context: dict[str, Any] = request.context or {}
        retrieved = context.get("retrieved_context") or context.get("retrieved") or []
        return cls(
            source=request.source,
            text=request.text,
            tenant_key=identity.tenant_key,
            app_id=identity.app_id,
            principal_id=identity.subject_id,
            open_id=identity.open_id,
            user_id=identity.user_id,
            union_id=identity.union_id,
            chat_id=request.chat_id,
            thread_id=request.thread_id,
            root_id=request.thread_id,
            original_message_id=request.message_id,
            original_event_id=request.event_id,
            retrieved_result_refs=[
                str(item.get("result_ref"))
                for item in retrieved
                if isinstance(item, dict) and item.get("result_ref")
            ],
        )


def parse_resume_payload(raw: str) -> OAuthResumeRequest:
    """解析恢复载荷；兼容历史纯文本记录。"""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return OAuthResumeRequest.from_legacy_text(raw or "")
    if isinstance(data, dict) and data.get("version") == 1:
        return OAuthResumeRequest.model_validate(data)
    return OAuthResumeRequest.from_legacy_text(raw or "")


def resume_payload_json(request: UserRequest) -> str:
    """把 UserRequest 序列化为可持久化的恢复载荷（给 create_authorization_url）。"""
    return OAuthResumeRequest.from_request(request).to_json()
