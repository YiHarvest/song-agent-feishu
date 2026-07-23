"""
状态存储模块。

使用 JSON 文件持久化用户记录、OAuth 令牌和消息处理状态。
支持版本迁移和原子写入。
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from .models import DailyRecord, DocumentBinding, OAuthToken


def _empty_state() -> dict[str, Any]:
    return {
        "version": 2,
        "records": {},
        "auth": {},
        "processed_message_ids": [],
        "p2p_chat_ids": {},
        "group_chat_ids": [],
        "document_bindings": {},
    }


class JsonStore:
    """
    JSON 文件状态存储。

    持久化用户计划记录、OAuth 令牌和消息处理状态。
    支持原子写入和版本迁移。
    """

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.state = _empty_state()
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            await self._persist()
            return
        raw = await asyncio.to_thread(self.path.read_text, encoding="utf-8")
        loaded = json.loads(raw)
        self.state = self._migrate(loaded)
        await self._persist()

    @staticmethod
    def record_key(chat_id: str, user_id: str, date: str) -> str:
        return f"{chat_id}:{user_id}:{date}"

    def get_record(self, chat_id: str, user_id: str, date: str) -> DailyRecord | None:
        value = self.state["records"].get(self.record_key(chat_id, user_id, date))
        return DailyRecord.model_validate(value) if value else None

    async def save_record(self, record: DailyRecord) -> None:
        async with self._lock:
            self.state["records"][record.key] = record.model_dump()
            await self._persist()

    async def delete_record(self, chat_id: str, user_id: str, date: str) -> None:
        async with self._lock:
            self.state["records"].pop(self.record_key(chat_id, user_id, date), None)
            await self._persist()

    def get_token(self, user_id: str) -> OAuthToken | None:
        value = self.state["auth"].get(user_id)
        return OAuthToken.model_validate(value) if value else None

    async def save_token(self, token: OAuthToken) -> None:
        async with self._lock:
            self.state["auth"][token.user_id] = token.model_dump()
            await self._persist()

    def has_processed_message(self, message_id: str) -> bool:
        return message_id in self.state["processed_message_ids"]

    async def mark_processed(self, message_id: str) -> None:
        async with self._lock:
            ids = self.state["processed_message_ids"]
            if message_id not in ids:
                ids.append(message_id)
                self.state["processed_message_ids"] = ids[-2000:]
                await self._persist()

    def group_chat_ids(self) -> set[str]:
        return set(self.state["group_chat_ids"])

    async def add_group_chat_id(self, chat_id: str) -> None:
        async with self._lock:
            ids = set(self.state["group_chat_ids"])
            ids.add(chat_id)
            self.state["group_chat_ids"] = sorted(ids)
            await self._persist()

    def p2p_chat_ids(self) -> dict[str, str]:
        return dict(self.state["p2p_chat_ids"])

    async def save_p2p_chat_id(self, user_id: str, chat_id: str) -> None:
        async with self._lock:
            self.state["p2p_chat_ids"][user_id] = chat_id
            await self._persist()

    @staticmethod
    def document_binding_key(chat_id: str, user_id: str) -> str:
        return f"{chat_id}:{user_id}"

    def get_document_binding(self, chat_id: str, user_id: str) -> DocumentBinding | None:
        value = self.state["document_bindings"].get(self.document_binding_key(chat_id, user_id))
        return DocumentBinding.model_validate(value) if value else None

    async def save_document_binding(self, binding: DocumentBinding) -> None:
        async with self._lock:
            key = self.document_binding_key(binding.chat_id, binding.user_id)
            self.state["document_bindings"][key] = binding.model_dump()
            await self._persist()

    async def _persist(self) -> None:
        payload = json.dumps(self.state, ensure_ascii=False, indent=2) + "\n"
        temporary = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")

        def write() -> None:
            temporary.write_text(payload, encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(self.path)

        await asyncio.to_thread(write)

    @staticmethod
    def _migrate(value: dict[str, Any]) -> dict[str, Any]:
        if value.get("version") == 2:
            return {**_empty_state(), **value}
        state = _empty_state()
        state["processed_message_ids"] = value.get("processedMessageIds", [])
        state["group_chat_ids"] = value.get("groupChatIds", [])
        binding = value.get("binding") or {}
        if binding.get("userId") and binding.get("chatId"):
            state["p2p_chat_ids"][binding["userId"]] = binding["chatId"]
        for user_id, token in value.get("auth", {}).items():
            state["auth"][user_id] = {
                "user_id": user_id,
                "access_token": token.get("accessToken", ""),
                "refresh_token": token.get("refreshToken", ""),
                "expires_at": token.get("expiresAt", 0),
                "refresh_expires_at": token.get("refreshExpiresAt", 0),
                "scope": token.get("scope", ""),
            }
        return state
