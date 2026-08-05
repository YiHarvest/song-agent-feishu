import asyncio
import json
import time
from pathlib import Path

import pytest

from song_agent.models import DailyRecord, DocumentBinding, FeishuIdentity, OAuthToken, PlanTask
from song_agent.services.encryption import AesGcmTokenCipher
from song_agent.store import SqliteStore


def token_cipher(version: int = 1) -> AesGcmTokenCipher:
    return AesGcmTokenCipher({version: bytes([version]) * 32}, version)


async def make_store(tmp_path: Path, *, legacy: Path | None = None) -> SqliteStore:
    store = SqliteStore(
        tmp_path / "state.db",
        app_id="cli-test",
        token_cipher=token_cipher(),
        legacy_json_path=legacy,
    )
    await store.initialize()
    return store


@pytest.mark.asyncio
async def test_records_are_isolated_by_tenant_app_chat_thread_user_and_date(tmp_path: Path) -> None:
    store = await make_store(tmp_path)
    try:
        for user_id, title in (("user-a", "A 的提醒"), ("user-b", "B 的提醒")):
            record = DailyRecord(
                key=store.record_key(
                    "group",
                    user_id,
                    "2026-07-22",
                    tenant_key="tenant",
                    thread_id="thread",
                ),
                date="2026-07-22",
                tenant_key="tenant",
                app_id="cli-test",
                chat_id="group",
                thread_id="thread",
                user_id=user_id,
                plan_status="draft",
                tasks=[PlanTask(id="A1", priority="A", title=title, start_time="20:00")],
                created_at="now",
                updated_at="now",
            )
            await store.save_record(record)
        first = await store.get_record(
            "group",
            "user-a",
            "2026-07-22",
            tenant_key="tenant",
            thread_id="thread",
        )
        second = await store.get_record(
            "group",
            "user-b",
            "2026-07-22",
            tenant_key="tenant",
            thread_id="thread",
        )
        other_thread = await store.get_record(
            "group",
            "user-a",
            "2026-07-22",
            tenant_key="tenant",
            thread_id="other",
        )
        assert first and first.tasks[0].title == "A 的提醒"
        assert second and second.tasks[0].title == "B 的提醒"
        assert other_thread is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_oauth_tokens_are_isolated_per_tenant_app_and_user(tmp_path: Path) -> None:
    store = await make_store(tmp_path)
    try:
        now = int(time.time() * 1000)
        for user_id in ("user-a", "user-b"):
            await store.save_token(
                OAuthToken(
                    user_id=user_id,
                    tenant_key="tenant",
                    app_id="cli-test",
                    open_id=f"open-{user_id}",
                    access_token=f"access-{user_id}",
                    refresh_token=f"refresh-{user_id}",
                    expires_at=now + 1_000_000,
                    refresh_expires_at=now + 2_000_000,
                    scope="calendar:calendar drive:drive docx:document",
                )
            )
        first = await store.get_token("user-a", tenant_key="tenant")
        second = await store.get_token("user-b", tenant_key="tenant")
        other_tenant = await store.get_token("user-a", tenant_key="other")
        assert first and first.access_token == "access-user-a"
        assert second and second.access_token == "access-user-b"
        assert other_tenant is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_oauth_refresh_lease_allows_only_one_process(tmp_path: Path) -> None:
    first = await make_store(tmp_path)
    second = SqliteStore(
        tmp_path / "state.db",
        app_id="cli-test",
        token_cipher=token_cipher(),
    )
    await second.initialize()
    try:
        now = int(time.time() * 1000)
        original = OAuthToken(
            user_id="user-a",
            tenant_key="tenant",
            app_id="cli-test",
            open_id="open-a",
            access_token="expired",
            refresh_token="refresh-a",
            expires_at=now - 1,
            refresh_expires_at=now + 1_000_000,
            scope="calendar:calendar",
        )
        await first.save_token(original)
        claims = await asyncio.gather(
            first.claim_token_refresh(
                "user-a",
                tenant_key="tenant",
                app_id="cli-test",
                owner_id="worker-a",
            ),
            second.claim_token_refresh(
                "user-a",
                tenant_key="tenant",
                app_id="cli-test",
                owner_id="worker-b",
            ),
        )
        assert sum(item is not None for item in claims) == 1
        winner = "worker-a" if claims[0] else "worker-b"
        winning_store = first if claims[0] else second
        refreshed = original.model_copy(
            update={
                "access_token": "fresh",
                "expires_at": now + 1_000_000,
            }
        )
        assert await winning_store.save_refreshed_token(refreshed, owner_id=winner)
        stored = await second.get_token("user-a", tenant_key="tenant")
        assert stored and stored.access_token == "fresh"
        row = await (
            await second.db.execute(
                """
                SELECT refresh_status, refresh_attempts, token_version
                FROM oauth_tokens
                WHERE tenant_key = 'tenant' AND app_id = 'cli-test'
                  AND subject_id = 'user-a'
                """
            )
        ).fetchone()
        assert row["refresh_status"] == "idle"
        assert row["refresh_attempts"] == 1
        assert row["token_version"] == 2
    finally:
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_document_bindings_are_isolated_by_thread_and_user(tmp_path: Path) -> None:
    store = await make_store(tmp_path)
    try:
        await store.save_document_binding(
            DocumentBinding(
                chat_id="chat-a",
                user_id="user-a",
                title="每日计划",
                token="doc-token",
                url="https://feishu.cn/docx/doc-token",
            ),
            tenant_key="tenant",
            thread_id="thread-a",
        )
        found = await store.get_document_binding(
            "chat-a",
            "user-a",
            tenant_key="tenant",
            thread_id="thread-a",
        )
        wrong_thread = await store.get_document_binding(
            "chat-a",
            "user-a",
            tenant_key="tenant",
            thread_id="thread-b",
        )
        assert found and found.token == "doc-token"
        assert wrong_thread is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_event_claim_is_atomic_under_concurrency(tmp_path: Path) -> None:
    store = await make_store(tmp_path)
    try:
        results = await asyncio.gather(*(store.claim_event("event-1") for _ in range(20)))
        assert results.count(True) == 1
        assert results.count(False) == 19
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_sqlite_uses_wal(tmp_path: Path) -> None:
    store = await make_store(tmp_path)
    try:
        cursor = await store.db.execute("PRAGMA journal_mode")
        row = await cursor.fetchone()
        assert row[0].lower() == "wal"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_oauth_state_is_persisted_hashed_and_single_use(tmp_path: Path) -> None:
    store = await make_store(tmp_path)
    try:
        identity = FeishuIdentity(
            tenant_key="tenant",
            app_id="cli-test",
            open_id="open-a",
            union_id="union-a",
        )
        await store.save_oauth_authorization(
            "raw-secret-state",
            identity,
            "chat",
            int(time.time()) + 600,
        )
        cursor = await store.db.execute("SELECT state_hash FROM oauth_authorizations")
        row = await cursor.fetchone()
        assert row and row["state_hash"] != "raw-secret-state"
        consumed = await store.consume_oauth_authorization("raw-secret-state")
        assert consumed and consumed[0].subject_id == "union-a" and consumed[1] == "chat"
        assert consumed[2] == ""  # original_request默认为空字符串
        assert await store.consume_oauth_authorization("raw-secret-state") is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_version_one_json_is_imported_once_without_shared_record(tmp_path: Path) -> None:
    legacy = tmp_path / "state.json"
    legacy.write_text(
        json.dumps(
            {
                "version": 1,
                "records": {"old-shared-record": {"unsafe": True}},
                "auth": {},
                "processedMessageIds": ["message-1"],
                "binding": {"chatId": "p2p-chat", "userId": "user-a"},
            }
        ),
        encoding="utf-8",
    )
    store = await make_store(tmp_path, legacy=legacy)
    try:
        # 正常启动不再自动导入旧 JSON；必须显式调用一次性迁移工具逻辑。
        imported = await store.import_legacy_json_once()
        assert imported["processed_events"] == 1
        # 幂等：第二次调用不再导入
        again = await store.import_legacy_json_once()
        assert again["processed_events"] == 0
        assert await store.has_processed_message("message-1")
        assert await store.p2p_chat_ids() == {"user-a": "p2p-chat"}
        assert await store.get_record("group", "user-a", "2026-07-22") is None
        assert legacy.exists()
    finally:
        await store.close()
