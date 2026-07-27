"""SQLite 持久化存储，支持 WAL、事务写入和旧版 JSON 迁移。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any

import aiosqlite

from .db.migrations import rotate_oauth_tokens, run_migrations
from .models import (
    ApiChannelBinding,
    DailyRecord,
    DocumentBinding,
    FeishuIdentity,
    OAuthToken,
    PendingAction,
    ScheduledJob,
)
from .services.encryption import (
    AesGcmTokenCipher,
    EncryptedValue,
    TokenCipherError,
    token_associated_data,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    tenant_key TEXT NOT NULL,
    app_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    open_id TEXT NOT NULL DEFAULT '',
    user_id TEXT NOT NULL DEFAULT '',
    union_id TEXT NOT NULL DEFAULT '',
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (tenant_key, app_id, subject_id)
);

CREATE TABLE IF NOT EXISTS oauth_tokens (
    tenant_key TEXT NOT NULL,
    app_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    open_id TEXT NOT NULL DEFAULT '',
    tenant_user_id TEXT NOT NULL DEFAULT '',
    union_id TEXT NOT NULL DEFAULT '',
    access_token TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    access_token_ciphertext BLOB,
    access_token_nonce BLOB,
    refresh_token_ciphertext BLOB,
    refresh_token_nonce BLOB,
    encryption_key_version INTEGER,
    disabled_at INTEGER,
    refresh_status TEXT NOT NULL DEFAULT 'idle',
    refresh_lease_owner TEXT,
    refresh_lease_expires_at INTEGER,
    refresh_attempts INTEGER NOT NULL DEFAULT 0,
    token_version INTEGER NOT NULL DEFAULT 1,
    last_refresh_success_at INTEGER,
    last_refresh_failure_at INTEGER,
    expires_at INTEGER NOT NULL,
    refresh_expires_at INTEGER NOT NULL,
    scope TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (tenant_key, app_id, subject_id)
);

CREATE TABLE IF NOT EXISTS oauth_authorizations (
    state_hash TEXT PRIMARY KEY,
    tenant_key TEXT NOT NULL,
    app_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    open_id TEXT NOT NULL,
    tenant_user_id TEXT NOT NULL DEFAULT '',
    union_id TEXT NOT NULL DEFAULT '',
    chat_id TEXT NOT NULL,
    original_request TEXT NOT NULL DEFAULT '',
    expires_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    conversation_key TEXT PRIMARY KEY,
    tenant_key TEXT NOT NULL,
    app_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    thread_id TEXT NOT NULL DEFAULT '',
    subject_id TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_plans (
    record_key TEXT PRIMARY KEY,
    tenant_key TEXT NOT NULL,
    app_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    thread_id TEXT NOT NULL DEFAULT '',
    subject_id TEXT NOT NULL,
    plan_date TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (tenant_key, app_id, chat_id, thread_id, subject_id, plan_date)
);

CREATE TABLE IF NOT EXISTS pending_actions (
    action_id TEXT PRIMARY KEY,
    tenant_key TEXT NOT NULL,
    app_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    thread_id TEXT NOT NULL DEFAULT '',
    creator_subject_id TEXT NOT NULL,
    creator_open_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    source_message_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'awaiting_confirmation', 'confirmed', 'executing', 'succeeded',
        'failed_retryable', 'failed_final', 'unknown_remote_state',
        'cancelled', 'expired'
    )),
    expires_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    consumed_at INTEGER,
    confirmed_at INTEGER,
    completed_at INTEGER,
    claimed_by TEXT,
    claim_expires_at INTEGER,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    remote_resource_id TEXT NOT NULL DEFAULT '',
    remote_request_id TEXT NOT NULL DEFAULT '',
    last_error_code TEXT NOT NULL DEFAULT '',
    last_error_message TEXT NOT NULL DEFAULT '',
    payload_version INTEGER NOT NULL DEFAULT 1,
    idempotency_key TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'legacy_agent',
    source_card_message_id TEXT NOT NULL DEFAULT '',
    result_json TEXT NOT NULL DEFAULT '{}',
    updated_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS action_attempts (
    attempt_id TEXT PRIMARY KEY,
    action_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    started_at INTEGER NOT NULL,
    completed_at INTEGER,
    status TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    remote_request_id TEXT NOT NULL DEFAULT '',
    remote_resource_id TEXT NOT NULL DEFAULT '',
    error_code TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(action_id) REFERENCES pending_actions(action_id)
);

CREATE TABLE IF NOT EXISTS action_outbox (
    outbox_id TEXT PRIMARY KEY,
    action_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    available_at INTEGER NOT NULL,
    claimed_by TEXT,
    claim_expires_at INTEGER,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    processed_at INTEGER,
    FOREIGN KEY(action_id) REFERENCES pending_actions(action_id)
);

CREATE TABLE IF NOT EXISTS scheduled_jobs (
    job_id TEXT PRIMARY KEY,
    tenant_key TEXT NOT NULL,
    app_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    job_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    timezone TEXT NOT NULL,
    cron_expression TEXT NOT NULL DEFAULT '',
    run_at INTEGER NOT NULL,
    status TEXT NOT NULL,
    claimed_by TEXT,
    claim_expires_at INTEGER,
    fencing_token INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_retry_at INTEGER,
    last_error TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    completed_at INTEGER,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS scheduler_leases (
    lease_name TEXT PRIMARY KEY,
    holder_id TEXT NOT NULL,
    acquired_at INTEGER NOT NULL,
    renewed_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    fencing_token INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS processed_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    processed_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_logs (
    audit_id TEXT PRIMARY KEY,
    occurred_at INTEGER NOT NULL,
    trace_id TEXT NOT NULL,
    span_id TEXT,
    tenant_key TEXT,
    app_id TEXT,
    principal_id TEXT,
    chat_id TEXT,
    thread_id TEXT,
    message_id TEXT,
    agent_run_id TEXT,
    action_id TEXT,
    job_id TEXT,
    operation TEXT NOT NULL,
    resource_type TEXT,
    resource_id TEXT,
    decision TEXT,
    risk_level TEXT,
    result TEXT NOT NULL,
    error_code TEXT,
    request_fingerprint TEXT,
    payload_hash TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS agent_runs (
    run_id TEXT PRIMARY KEY,
    tenant_key TEXT NOT NULL,
    app_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    conversation_key TEXT NOT NULL,
    inbound_message_id TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL,
    step_count INTEGER NOT NULL DEFAULT 0,
    tool_call_count INTEGER NOT NULL DEFAULT 0,
    started_at INTEGER NOT NULL,
    completed_at INTEGER,
    final_response TEXT NOT NULL DEFAULT '',
    error_code TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS agent_steps (
    step_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    step_index INTEGER NOT NULL,
    decision_type TEXT NOT NULL,
    decision_summary TEXT NOT NULL DEFAULT '',
    tool_name TEXT NOT NULL DEFAULT '',
    arguments_hash TEXT NOT NULL DEFAULT '',
    arguments_summary TEXT NOT NULL DEFAULT '',
    result_summary TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    started_at INTEGER NOT NULL,
    completed_at INTEGER,
    UNIQUE(run_id, step_index),
    FOREIGN KEY(run_id) REFERENCES agent_runs(run_id)
);

CREATE TABLE IF NOT EXISTS group_chats (
    tenant_key TEXT NOT NULL,
    app_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (tenant_key, app_id, chat_id)
);

CREATE TABLE IF NOT EXISTS p2p_chats (
    tenant_key TEXT NOT NULL,
    app_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (tenant_key, app_id, subject_id)
);

CREATE TABLE IF NOT EXISTS document_bindings (
    tenant_key TEXT NOT NULL,
    app_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    thread_id TEXT NOT NULL DEFAULT '',
    subject_id TEXT NOT NULL,
    title TEXT NOT NULL,
    token TEXT NOT NULL,
    url TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (tenant_key, app_id, chat_id, thread_id, subject_id)
);

CREATE TABLE IF NOT EXISTS conversation_messages (
    row_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    tenant_key TEXT NOT NULL,
    app_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    chat_id TEXT NOT NULL DEFAULT '',
    thread_id TEXT NOT NULL DEFAULT '',
    message_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'tool', 'system')),
    content TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    summarized_at INTEGER,
    UNIQUE (tenant_key, app_id, principal_id, message_id)
);

CREATE TABLE IF NOT EXISTS conversation_summaries (
    summary_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    tenant_key TEXT NOT NULL,
    app_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    covered_message_count INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE (tenant_key, app_id, principal_id, session_id)
);

CREATE TABLE IF NOT EXISTS user_memories (
    memory_id TEXT PRIMARY KEY,
    tenant_key TEXT NOT NULL,
    app_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    memory_type TEXT NOT NULL,
    memory_key TEXT NOT NULL,
    memory_value TEXT NOT NULL,
    confidence REAL NOT NULL,
    source_message_id TEXT NOT NULL DEFAULT '',
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (tenant_key, app_id, principal_id, memory_type, memory_key)
);

CREATE TABLE IF NOT EXISTS tool_results (
    result_ref TEXT PRIMARY KEY,
    tenant_key TEXT NOT NULL,
    app_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    summary TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    truncated INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    expires_at INTEGER
);

CREATE TABLE IF NOT EXISTS attachments (
    attachment_id TEXT PRIMARY KEY,
    tenant_key TEXT NOT NULL,
    app_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    source TEXT NOT NULL,
    source_message_id TEXT NOT NULL,
    source_resource_key TEXT NOT NULL DEFAULT '',
    attachment_kind TEXT NOT NULL CHECK (
        attachment_kind IN ('image', 'audio', 'document', 'unknown')
    ),
    filename TEXT NOT NULL,
    media_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    storage_path TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('downloading', 'ready', 'parsing', 'parsed', 'failed', 'expired')
    ),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    expires_at INTEGER
);

CREATE TABLE IF NOT EXISTS api_idempotency (
    tenant_key TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('processing', 'completed')),
    response_json TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    PRIMARY KEY (tenant_key, principal_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS api_binding_codes (
    code_hash TEXT PRIMARY KEY,
    tenant_key TEXT NOT NULL,
    app_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    consumed_at INTEGER
);

CREATE TABLE IF NOT EXISTS api_channel_bindings (
    binding_id TEXT PRIMARY KEY,
    tenant_key TEXT NOT NULL,
    app_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    external_tenant_key TEXT NOT NULL,
    external_app_id TEXT NOT NULL,
    external_subject_id TEXT NOT NULL,
    external_open_id TEXT NOT NULL,
    external_user_id TEXT NOT NULL DEFAULT '',
    external_union_id TEXT NOT NULL DEFAULT '',
    external_chat_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_processed_events_time ON processed_events(processed_at);
CREATE INDEX IF NOT EXISTS idx_pending_actions_expiry ON pending_actions(status, expires_at);
CREATE INDEX IF NOT EXISTS idx_action_outbox_ready ON action_outbox(status, available_at);
CREATE INDEX IF NOT EXISTS idx_oauth_authorizations_expiry ON oauth_authorizations(expires_at);
CREATE INDEX IF NOT EXISTS idx_agent_runs_principal
ON agent_runs(tenant_key, app_id, principal_id, started_at);
CREATE INDEX IF NOT EXISTS idx_conversation_messages_recent
ON conversation_messages(tenant_key, app_id, principal_id, session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_user_memories_principal
ON user_memories(tenant_key, app_id, principal_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_attachments_owner
ON attachments(tenant_key, app_id, principal_id, created_at);
CREATE INDEX IF NOT EXISTS idx_attachments_message
ON attachments(tenant_key, source_message_id);
CREATE INDEX IF NOT EXISTS idx_api_bindings_owner
ON api_channel_bindings(tenant_key, app_id, principal_id, created_at);
"""


class SqliteStore:
    """异步 SQLite 存储库。

    每个应用实例使用一个连接。WAL 和 SQLite 唯一约束确保指向同一数据库的多个进程安全运行。
    """

    def __init__(
        self,
        path: Path,
        *,
        app_id: str = "",
        token_cipher: AesGcmTokenCipher,
        legacy_json_path: Path | None = None,
        event_retention_days: int = 30,
    ) -> None:
        self.path = path.resolve()
        self.app_id = app_id
        self.token_cipher = token_cipher
        self.legacy_json_path = legacy_json_path.resolve() if legacy_json_path else None
        self.event_retention_days = event_retention_days
        self._db: aiosqlite.Connection | None = None
        self._transaction_lock = asyncio.Lock()

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("SqliteStore.initialize() must be called first")
        return self._db

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 自动提交单条语句；多语句不变量使用显式 BEGIN IMMEDIATE 和 _transaction_lock。
        # 这防止不相关的异步任务共享同一个隐式事务。
        self._db = await aiosqlite.connect(
            self.path,
            timeout=10,
            isolation_level=None,
        )
        self._db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA journal_mode=WAL")
        await self.db.execute("PRAGMA synchronous=NORMAL")
        await self.db.execute("PRAGMA foreign_keys=ON")
        await self.db.execute("PRAGMA busy_timeout=10000")
        await self.db.executescript(SCHEMA)
        await run_migrations(self.db, self.token_cipher)
        await self._ensure_schema_upgrades()
        cutoff = int(time.time()) - self.event_retention_days * 86400
        await self.db.execute("DELETE FROM processed_events WHERE processed_at < ?", (cutoff,))
        await self.db.commit()
        self.path.chmod(0o600)
        await self._import_legacy_json_once()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def _ensure_schema_upgrades(self) -> None:
        cursor = await self.db.execute("PRAGMA table_info(pending_actions)")
        columns = {row["name"] for row in await cursor.fetchall()}
        if "creator_open_id" not in columns:
            await self.db.execute(
                "ALTER TABLE pending_actions ADD COLUMN creator_open_id TEXT NOT NULL DEFAULT ''"
            )

    def record_key(
        self,
        chat_id: str,
        user_id: str,
        date: str,
        *,
        tenant_key: str = "",
        app_id: str = "",
        thread_id: str = "",
    ) -> str:
        return ":".join(
            (
                tenant_key or "_",
                app_id or self.app_id or "_",
                chat_id,
                thread_id or "_",
                user_id,
                date,
            )
        )

    async def get_record(
        self,
        chat_id: str,
        user_id: str,
        date: str,
        *,
        tenant_key: str = "",
        app_id: str = "",
        thread_id: str = "",
    ) -> DailyRecord | None:
        cursor = await self.db.execute(
            """
            SELECT payload_json FROM daily_plans
            WHERE tenant_key = ? AND app_id = ? AND chat_id = ? AND thread_id = ?
              AND subject_id = ? AND plan_date = ?
            """,
            (tenant_key, app_id or self.app_id, chat_id, thread_id, user_id, date),
        )
        row = await cursor.fetchone()
        return DailyRecord.model_validate_json(row["payload_json"]) if row else None

    async def get_record_by_key(self, record_key: str) -> DailyRecord | None:
        cursor = await self.db.execute(
            "SELECT payload_json FROM daily_plans WHERE record_key = ?",
            (record_key,),
        )
        row = await cursor.fetchone()
        return DailyRecord.model_validate_json(row["payload_json"]) if row else None

    async def save_record(self, record: DailyRecord) -> None:
        await self.db.execute(
            """
            INSERT INTO daily_plans (
                record_key, tenant_key, app_id, chat_id, thread_id, subject_id,
                plan_date, payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tenant_key, app_id, chat_id, thread_id, subject_id, plan_date)
            DO UPDATE SET
                record_key = excluded.record_key,
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            (
                record.key,
                record.tenant_key,
                record.app_id or self.app_id,
                record.chat_id,
                record.thread_id,
                record.user_id,
                record.date,
                record.model_dump_json(),
                record.created_at,
                record.updated_at,
            ),
        )
        await self.db.commit()

    async def delete_record(
        self,
        chat_id: str,
        user_id: str,
        date: str,
        *,
        tenant_key: str = "",
        app_id: str = "",
        thread_id: str = "",
    ) -> None:
        await self.db.execute(
            """
            DELETE FROM daily_plans
            WHERE tenant_key = ? AND app_id = ? AND chat_id = ? AND thread_id = ?
              AND subject_id = ? AND plan_date = ?
            """,
            (tenant_key, app_id or self.app_id, chat_id, thread_id, user_id, date),
        )
        await self.db.commit()

    async def get_token(
        self,
        user_id: str,
        *,
        tenant_key: str = "",
        app_id: str = "",
    ) -> OAuthToken | None:
        cursor = await self.db.execute(
            """
            SELECT * FROM oauth_tokens
            WHERE tenant_key = ? AND app_id = ? AND subject_id = ?
            """,
            (tenant_key, app_id or self.app_id, user_id),
        )
        row = await cursor.fetchone()
        if not row or row["disabled_at"] is not None:
            return None
        try:
            return self._token_from_row(row)
        except TokenCipherError:
            await self.db.execute(
                """
                UPDATE oauth_tokens SET disabled_at = ?, updated_at = ?
                WHERE tenant_key = ? AND app_id = ? AND subject_id = ?
                """,
                (
                    int(time.time()),
                    int(time.time()),
                    row["tenant_key"],
                    row["app_id"],
                    row["subject_id"],
                ),
            )
            await self.db.commit()
            return None

    async def save_token(self, token: OAuthToken) -> None:
        access, refresh = self._encrypt_token(token)
        await self.db.execute(
            """
            INSERT INTO oauth_tokens (
                tenant_key, app_id, subject_id, open_id, tenant_user_id, union_id,
                access_token, refresh_token,
                access_token_ciphertext, access_token_nonce,
                refresh_token_ciphertext, refresh_token_nonce, encryption_key_version,
                expires_at, refresh_expires_at, scope, updated_at, disabled_at
            ) VALUES (?, ?, ?, ?, ?, ?, '', '', ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(tenant_key, app_id, subject_id) DO UPDATE SET
                open_id = excluded.open_id,
                tenant_user_id = excluded.tenant_user_id,
                union_id = excluded.union_id,
                access_token = '',
                refresh_token = '',
                access_token_ciphertext = excluded.access_token_ciphertext,
                access_token_nonce = excluded.access_token_nonce,
                refresh_token_ciphertext = excluded.refresh_token_ciphertext,
                refresh_token_nonce = excluded.refresh_token_nonce,
                encryption_key_version = excluded.encryption_key_version,
                expires_at = excluded.expires_at,
                refresh_expires_at = excluded.refresh_expires_at,
                scope = excluded.scope,
                updated_at = excluded.updated_at,
                disabled_at = NULL,
                refresh_status = 'idle',
                refresh_lease_owner = NULL,
                refresh_lease_expires_at = NULL,
                token_version = oauth_tokens.token_version + 1
            """,
            (
                token.tenant_key,
                token.app_id or self.app_id,
                token.user_id,
                token.open_id,
                token.tenant_user_id,
                token.union_id,
                access.ciphertext,
                access.nonce,
                refresh.ciphertext,
                refresh.nonce,
                access.key_version,
                token.expires_at,
                token.refresh_expires_at,
                token.scope,
                int(time.time()),
            ),
        )
        await self.db.commit()

    async def claim_token_refresh(
        self,
        user_id: str,
        *,
        tenant_key: str,
        app_id: str,
        owner_id: str,
        lease_seconds: int = 30,
    ) -> OAuthToken | None:
        now = int(time.time())
        async with self._transaction_lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self.db.execute(
                    """
                    UPDATE oauth_tokens
                    SET refresh_status = 'refreshing',
                        refresh_lease_owner = ?,
                        refresh_lease_expires_at = ?,
                        refresh_attempts = refresh_attempts + 1
                    WHERE tenant_key = ? AND app_id = ? AND subject_id = ?
                      AND disabled_at IS NULL
                      AND (
                          refresh_status != 'refreshing'
                          OR refresh_lease_expires_at IS NULL
                          OR refresh_lease_expires_at <= ?
                          OR refresh_lease_owner = ?
                      )
                    """,
                    (
                        owner_id,
                        now + lease_seconds,
                        tenant_key,
                        app_id or self.app_id,
                        user_id,
                        now,
                        owner_id,
                    ),
                )
                row = None
                if cursor.rowcount == 1:
                    row = await (
                        await self.db.execute(
                            """
                            SELECT * FROM oauth_tokens
                            WHERE tenant_key = ? AND app_id = ? AND subject_id = ?
                            """,
                            (tenant_key, app_id or self.app_id, user_id),
                        )
                    ).fetchone()
                await self.db.commit()
            except Exception:
                await self.db.rollback()
                raise
        return self._token_from_row(row) if row is not None else None

    async def save_refreshed_token(self, token: OAuthToken, *, owner_id: str) -> bool:
        access, refresh = self._encrypt_token(token)
        now = int(time.time())
        async with self._transaction_lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self.db.execute(
                    """
                    UPDATE oauth_tokens
                    SET open_id = ?, tenant_user_id = ?, union_id = ?,
                        access_token = '', refresh_token = '',
                        access_token_ciphertext = ?, access_token_nonce = ?,
                        refresh_token_ciphertext = ?, refresh_token_nonce = ?,
                        encryption_key_version = ?, expires_at = ?,
                        refresh_expires_at = ?, scope = ?, updated_at = ?,
                        refresh_status = 'idle', refresh_lease_owner = NULL,
                        refresh_lease_expires_at = NULL,
                        token_version = token_version + 1,
                        last_refresh_success_at = ?, disabled_at = NULL
                    WHERE tenant_key = ? AND app_id = ? AND subject_id = ?
                      AND refresh_status = 'refreshing'
                      AND refresh_lease_owner = ?
                      AND refresh_lease_expires_at > ?
                    """,
                    (
                        token.open_id,
                        token.tenant_user_id,
                        token.union_id,
                        access.ciphertext,
                        access.nonce,
                        refresh.ciphertext,
                        refresh.nonce,
                        access.key_version,
                        token.expires_at,
                        token.refresh_expires_at,
                        token.scope,
                        now,
                        now,
                        token.tenant_key,
                        token.app_id or self.app_id,
                        token.user_id,
                        owner_id,
                        now,
                    ),
                )
                await self.db.commit()
                return cursor.rowcount == 1
            except Exception:
                await self.db.rollback()
                raise

    async def fail_token_refresh(
        self,
        user_id: str,
        *,
        tenant_key: str,
        app_id: str,
        owner_id: str,
    ) -> bool:
        now = int(time.time())
        cursor = await self.db.execute(
            """
            UPDATE oauth_tokens
            SET refresh_status = 'failed', refresh_lease_owner = NULL,
                refresh_lease_expires_at = NULL, last_refresh_failure_at = ?,
                updated_at = ?
            WHERE tenant_key = ? AND app_id = ? AND subject_id = ?
              AND refresh_status = 'refreshing' AND refresh_lease_owner = ?
            """,
            (
                now,
                now,
                tenant_key,
                app_id or self.app_id,
                user_id,
                owner_id,
            ),
        )
        await self.db.commit()
        return cursor.rowcount == 1

    async def rotate_token_encryption(self) -> int:
        return await rotate_oauth_tokens(self.db, self.token_cipher)

    async def save_oauth_authorization(
        self,
        state: str,
        identity: FeishuIdentity,
        chat_id: str,
        expires_at: int,
        original_request: str = "",
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO oauth_authorizations(
                state_hash, tenant_key, app_id, subject_id, open_id,
                tenant_user_id, union_id, chat_id, original_request, expires_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _secret_hash(state),
                identity.tenant_key,
                identity.app_id or self.app_id,
                identity.subject_id,
                identity.open_id,
                identity.user_id,
                identity.union_id,
                chat_id,
                original_request,
                expires_at,
                int(time.time()),
            ),
        )
        await self.db.commit()

    async def consume_oauth_authorization(
        self,
        state: str,
    ) -> tuple[FeishuIdentity, str, str] | None:
        state_hash = _secret_hash(state)
        now = int(time.time())
        await self.db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await self.db.execute(
                "SELECT * FROM oauth_authorizations WHERE state_hash = ?",
                (state_hash,),
            )
            row = await cursor.fetchone()
            await self.db.execute(
                "DELETE FROM oauth_authorizations WHERE state_hash = ? OR expires_at <= ?",
                (state_hash, now),
            )
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            raise
        if not row or row["expires_at"] <= now:
            return None
        return (
            FeishuIdentity(
                tenant_key=row["tenant_key"],
                app_id=row["app_id"],
                open_id=row["open_id"],
                user_id=row["tenant_user_id"],
                union_id=row["union_id"],
            ),
            row["chat_id"],
            row["original_request"] or "",
        )

    async def claim_event(
        self,
        event_id: str,
        event_type: str = "message",
        *,
        tenant_key: str = "",
        app_id: str = "",
    ) -> bool:
        """原子性地认领事件。返回 False 表示其他 worker 已处理。"""
        scoped_event_id = ":".join((tenant_key or "_", app_id or self.app_id or "_", event_id))
        cursor = await self.db.execute(
            """
            INSERT INTO processed_events(event_id, event_type, processed_at)
            VALUES (?, ?, ?)
            ON CONFLICT(event_id) DO NOTHING
            """,
            (scoped_event_id, event_type, int(time.time())),
        )
        await self.db.commit()
        return cursor.rowcount == 1

    async def has_processed_message(self, message_id: str) -> bool:
        scoped_event_id = ":".join(("_", self.app_id or "_", message_id))
        cursor = await self.db.execute(
            "SELECT 1 FROM processed_events WHERE event_id IN (?, ?)",
            (scoped_event_id, message_id),
        )
        return await cursor.fetchone() is not None

    async def mark_processed(self, message_id: str) -> None:
        await self.claim_event(message_id)

    async def group_chat_ids(self, *, tenant_key: str = "", app_id: str = "") -> set[str]:
        cursor = await self.db.execute(
            "SELECT chat_id FROM group_chats WHERE tenant_key = ? AND app_id = ?",
            (tenant_key, app_id or self.app_id),
        )
        return {row["chat_id"] for row in await cursor.fetchall()}

    async def add_group_chat_id(
        self, chat_id: str, *, tenant_key: str = "", app_id: str = ""
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO group_chats(tenant_key, app_id, chat_id, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (tenant_key, app_id or self.app_id, chat_id, int(time.time())),
        )
        await self.db.commit()

    async def p2p_chat_ids(self, *, tenant_key: str = "", app_id: str = "") -> dict[str, str]:
        cursor = await self.db.execute(
            "SELECT subject_id, chat_id FROM p2p_chats WHERE tenant_key = ? AND app_id = ?",
            (tenant_key, app_id or self.app_id),
        )
        return {row["subject_id"]: row["chat_id"] for row in await cursor.fetchall()}

    async def save_p2p_chat_id(
        self,
        user_id: str,
        chat_id: str,
        *,
        tenant_key: str = "",
        app_id: str = "",
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO p2p_chats(tenant_key, app_id, subject_id, chat_id, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(tenant_key, app_id, subject_id) DO UPDATE SET
                chat_id = excluded.chat_id,
                updated_at = excluded.updated_at
            """,
            (tenant_key, app_id or self.app_id, user_id, chat_id, int(time.time())),
        )
        await self.db.commit()

    async def acquire_scheduler_lease(
        self,
        lease_name: str,
        holder_id: str,
        *,
        lease_seconds: int,
    ) -> int | None:
        now = int(time.time())
        async with self._transaction_lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await self.db.execute(
                        "SELECT * FROM scheduler_leases WHERE lease_name = ?",
                        (lease_name,),
                    )
                ).fetchone()
                if row is None:
                    fencing_token = 1
                    await self.db.execute(
                        """
                        INSERT INTO scheduler_leases(
                            lease_name, holder_id, acquired_at, renewed_at,
                            expires_at, fencing_token
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            lease_name,
                            holder_id,
                            now,
                            now,
                            now + lease_seconds,
                            fencing_token,
                        ),
                    )
                elif row["holder_id"] == holder_id and row["expires_at"] > now:
                    fencing_token = row["fencing_token"]
                    await self.db.execute(
                        """
                        UPDATE scheduler_leases
                        SET renewed_at = ?, expires_at = ?
                        WHERE lease_name = ? AND holder_id = ? AND fencing_token = ?
                        """,
                        (
                            now,
                            now + lease_seconds,
                            lease_name,
                            holder_id,
                            fencing_token,
                        ),
                    )
                elif row["expires_at"] <= now:
                    fencing_token = row["fencing_token"] + 1
                    cursor = await self.db.execute(
                        """
                        UPDATE scheduler_leases
                        SET holder_id = ?, acquired_at = ?, renewed_at = ?,
                            expires_at = ?, fencing_token = ?
                        WHERE lease_name = ? AND fencing_token = ? AND expires_at <= ?
                        """,
                        (
                            holder_id,
                            now,
                            now,
                            now + lease_seconds,
                            fencing_token,
                            lease_name,
                            row["fencing_token"],
                            now,
                        ),
                    )
                    if cursor.rowcount != 1:
                        fencing_token = None
                else:
                    fencing_token = None
                await self.db.commit()
                return fencing_token
            except Exception:
                await self.db.rollback()
                raise

    async def validate_scheduler_lease(
        self,
        lease_name: str,
        holder_id: str,
        fencing_token: int,
    ) -> bool:
        cursor = await self.db.execute(
            """
            SELECT 1 FROM scheduler_leases
            WHERE lease_name = ? AND holder_id = ? AND fencing_token = ?
              AND expires_at > ?
            """,
            (lease_name, holder_id, fencing_token, int(time.time())),
        )
        return await cursor.fetchone() is not None

    async def upsert_scheduled_job(
        self,
        *,
        job_id: str,
        job_type: str,
        payload: dict[str, Any],
        timezone: str,
        cron_expression: str,
        run_at: int,
        tenant_key: str = "",
        app_id: str = "",
        principal_id: str = "*",
    ) -> None:
        now = int(time.time())
        await self.db.execute(
            """
            INSERT INTO scheduled_jobs(
                job_id, tenant_key, app_id, principal_id, job_type,
                payload_json, timezone, cron_expression, run_at, status,
                attempts, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'scheduled', 0, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                tenant_key = excluded.tenant_key,
                app_id = excluded.app_id,
                principal_id = excluded.principal_id,
                job_type = excluded.job_type,
                payload_json = excluded.payload_json,
                timezone = excluded.timezone,
                cron_expression = excluded.cron_expression,
                run_at = CASE
                    WHEN scheduled_jobs.status IN ('scheduled', 'claimed', 'retry')
                    THEN scheduled_jobs.run_at
                    ELSE excluded.run_at
                END,
                status = CASE
                    WHEN scheduled_jobs.status IN ('scheduled', 'claimed', 'retry')
                    THEN scheduled_jobs.status
                    ELSE 'scheduled'
                END,
                updated_at = excluded.updated_at
            """,
            (
                job_id,
                tenant_key,
                app_id or self.app_id,
                principal_id,
                job_type,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                timezone,
                cron_expression,
                run_at,
                now,
                now,
            ),
        )
        await self.db.commit()

    async def claim_due_scheduled_jobs(
        self,
        *,
        lease_name: str,
        holder_id: str,
        fencing_token: int,
        claim_seconds: int = 60,
        limit: int = 20,
    ) -> list[ScheduledJob]:
        now = int(time.time())
        async with self._transaction_lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                lease = await (
                    await self.db.execute(
                        """
                        SELECT 1 FROM scheduler_leases
                        WHERE lease_name = ? AND holder_id = ?
                          AND fencing_token = ? AND expires_at > ?
                        """,
                        (lease_name, holder_id, fencing_token, now),
                    )
                ).fetchone()
                if lease is None:
                    await self.db.rollback()
                    return []
                await self.db.execute(
                    """
                    UPDATE scheduled_jobs
                    SET status = 'retry', claimed_by = NULL,
                        claim_expires_at = NULL, next_retry_at = ?,
                        last_error = 'previous claim expired'
                    WHERE status = 'claimed' AND claim_expires_at <= ?
                    """,
                    (now, now),
                )
                rows = await (
                    await self.db.execute(
                        """
                        SELECT * FROM scheduled_jobs
                        WHERE (
                            status = 'scheduled' AND run_at <= ?
                        ) OR (
                            status = 'retry' AND next_retry_at <= ?
                        )
                        ORDER BY COALESCE(next_retry_at, run_at), created_at
                        LIMIT ?
                        """,
                        (now, now, limit),
                    )
                ).fetchall()
                claimed: list[ScheduledJob] = []
                for row in rows:
                    cursor = await self.db.execute(
                        """
                        UPDATE scheduled_jobs
                        SET status = 'claimed', claimed_by = ?,
                            claim_expires_at = ?, fencing_token = ?,
                            attempts = attempts + 1, updated_at = ?
                        WHERE job_id = ? AND status IN ('scheduled', 'retry')
                        """,
                        (
                            holder_id,
                            now + claim_seconds,
                            fencing_token,
                            now,
                            row["job_id"],
                        ),
                    )
                    if cursor.rowcount == 1:
                        claimed.append(
                            ScheduledJob(
                                job_id=row["job_id"],
                                tenant_key=row["tenant_key"],
                                app_id=row["app_id"],
                                principal_id=row["principal_id"],
                                job_type=row["job_type"],
                                payload=json.loads(row["payload_json"]),
                                timezone=row["timezone"],
                                cron_expression=row["cron_expression"],
                                run_at=row["run_at"],
                                status="claimed",
                                attempts=row["attempts"] + 1,
                                fencing_token=fencing_token,
                            )
                        )
                await self.db.commit()
                return claimed
            except Exception:
                await self.db.rollback()
                raise

    async def complete_scheduled_job(
        self,
        job_id: str,
        *,
        holder_id: str,
        fencing_token: int,
        next_run_at: int | None,
    ) -> bool:
        now = int(time.time())
        cursor = await self.db.execute(
            """
            UPDATE scheduled_jobs
            SET status = ?, run_at = COALESCE(?, run_at),
                claimed_by = NULL, claim_expires_at = NULL,
                next_retry_at = NULL, last_error = '',
                completed_at = CASE WHEN ? IS NULL THEN ? ELSE NULL END,
                updated_at = ?
            WHERE job_id = ? AND status = 'claimed'
              AND claimed_by = ? AND fencing_token = ?
              AND EXISTS (
                  SELECT 1 FROM scheduler_leases
                  WHERE lease_name = 'scheduler' AND holder_id = ?
                    AND fencing_token = ? AND expires_at > ?
              )
            """,
            (
                "scheduled" if next_run_at is not None else "completed",
                next_run_at,
                next_run_at,
                now,
                now,
                job_id,
                holder_id,
                fencing_token,
                holder_id,
                fencing_token,
                now,
            ),
        )
        await self.db.commit()
        return cursor.rowcount == 1

    async def fail_scheduled_job(
        self,
        job_id: str,
        *,
        holder_id: str,
        fencing_token: int,
        error: str,
        retry_delay_seconds: int,
    ) -> bool:
        now = int(time.time())
        cursor = await self.db.execute(
            """
            UPDATE scheduled_jobs
            SET status = 'retry', next_retry_at = ?,
                claimed_by = NULL, claim_expires_at = NULL,
                last_error = ?, updated_at = ?
            WHERE job_id = ? AND status = 'claimed'
              AND claimed_by = ? AND fencing_token = ?
            """,
            (
                now + retry_delay_seconds,
                error[:500],
                now,
                job_id,
                holder_id,
                fencing_token,
            ),
        )
        await self.db.commit()
        return cursor.rowcount == 1

    async def write_audit_log(
        self,
        *,
        trace_id: str,
        operation: str,
        result: str,
        tenant_key: str = "",
        app_id: str = "",
        principal_id: str = "",
        chat_id: str = "",
        thread_id: str = "",
        message_id: str = "",
        agent_run_id: str = "",
        action_id: str = "",
        decision: str = "",
        risk_level: str = "",
        payload_hash: str = "",
        metadata: Any = None,
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO audit_logs(
                audit_id, occurred_at, trace_id, tenant_key, app_id, principal_id,
                chat_id, thread_id, message_id, agent_run_id, action_id,
                operation, decision, risk_level, result, payload_hash, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                int(time.time()),
                trace_id,
                tenant_key,
                app_id or self.app_id,
                principal_id,
                chat_id,
                thread_id,
                message_id,
                agent_run_id,
                action_id,
                operation,
                decision,
                risk_level,
                result,
                payload_hash,
                json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
            ),
        )
        await self.db.commit()

    async def start_agent_run(
        self,
        *,
        run_id: str,
        tenant_key: str,
        app_id: str,
        principal_id: str,
        conversation_key: str,
        inbound_message_id: str,
        model: str,
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO agent_runs(
                run_id, tenant_key, app_id, principal_id, conversation_key,
                inbound_message_id, model, status, started_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?)
            """,
            (
                run_id,
                tenant_key,
                app_id or self.app_id,
                principal_id,
                conversation_key,
                inbound_message_id,
                model,
                int(time.time()),
            ),
        )
        await self.db.commit()

    async def recover_stale_agent_runs(self, *, max_age_seconds: int) -> int:
        now = int(time.time())
        cursor = await self.db.execute(
            """
            UPDATE agent_runs
            SET status = 'interrupted', completed_at = ?,
                error_code = 'process_interrupted'
            WHERE status = 'running' AND started_at <= ?
            """,
            (now, now - max_age_seconds),
        )
        await self.db.commit()
        return cursor.rowcount

    async def record_agent_step(
        self,
        *,
        run_id: str,
        step_index: int,
        decision_type: str,
        decision_summary: str,
        tool_name: str,
        arguments_hash: str,
        arguments_summary: str,
        status: str,
        result_summary: str = "",
        completed: bool = False,
    ) -> None:
        now = int(time.time())
        await self.db.execute(
            """
            INSERT INTO agent_steps(
                step_id, run_id, step_index, decision_type, decision_summary,
                tool_name, arguments_hash, arguments_summary, result_summary,
                status, started_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, step_index) DO UPDATE SET
                result_summary = excluded.result_summary,
                status = excluded.status,
                completed_at = excluded.completed_at
            """,
            (
                str(uuid.uuid4()),
                run_id,
                step_index,
                decision_type,
                decision_summary[:500],
                tool_name,
                arguments_hash,
                arguments_summary[:1000],
                result_summary[:2000],
                status,
                now,
                now if completed else None,
            ),
        )
        await self.db.commit()

    async def finish_agent_run(
        self,
        run_id: str,
        *,
        status: str,
        step_count: int,
        tool_call_count: int,
        final_response: str,
        error_code: str,
    ) -> None:
        await self.db.execute(
            """
            UPDATE agent_runs
            SET status = ?, step_count = ?, tool_call_count = ?,
                completed_at = ?, final_response = ?, error_code = ?
            WHERE run_id = ? AND status = 'running'
            """,
            (
                status,
                step_count,
                tool_call_count,
                int(time.time()),
                final_response[:4000],
                error_code,
                run_id,
            ),
        )
        await self.db.commit()

    async def get_document_binding(
        self,
        chat_id: str,
        user_id: str,
        *,
        tenant_key: str = "",
        app_id: str = "",
        thread_id: str = "",
    ) -> DocumentBinding | None:
        cursor = await self.db.execute(
            """
            SELECT title, token, url FROM document_bindings
            WHERE tenant_key = ? AND app_id = ? AND chat_id = ? AND thread_id = ?
              AND subject_id = ?
            """,
            (tenant_key, app_id or self.app_id, chat_id, thread_id, user_id),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return DocumentBinding(
            chat_id=chat_id,
            user_id=user_id,
            title=row["title"],
            token=row["token"],
            url=row["url"],
        )

    async def save_document_binding(
        self,
        binding: DocumentBinding,
        *,
        tenant_key: str = "",
        app_id: str = "",
        thread_id: str = "",
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO document_bindings(
                tenant_key, app_id, chat_id, thread_id, subject_id,
                title, token, url, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tenant_key, app_id, chat_id, thread_id, subject_id) DO UPDATE SET
                title = excluded.title,
                token = excluded.token,
                url = excluded.url,
                updated_at = excluded.updated_at
            """,
            (
                tenant_key,
                app_id or self.app_id,
                binding.chat_id,
                thread_id,
                binding.user_id,
                binding.title,
                binding.token,
                binding.url,
                int(time.time()),
            ),
        )
        await self.db.commit()

    async def save_pending_action(self, action: PendingAction) -> None:
        await self.db.execute(
            """
            INSERT INTO pending_actions(
                action_id, tenant_key, app_id, chat_id, thread_id,
                creator_subject_id, creator_open_id, action_type, payload_json,
                payload_hash, source_message_id, status, expires_at, created_at, consumed_at,
                payload_version, idempotency_key, source, source_card_message_id,
                result_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tenant_key, app_id, idempotency_key) WHERE idempotency_key != ''
            DO NOTHING
            """,
            (
                action.action_id,
                action.tenant_key,
                action.app_id or self.app_id,
                action.chat_id,
                action.thread_id,
                action.creator_subject_id,
                action.creator_open_id,
                action.action_type,
                json.dumps(action.payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                action.payload_hash,
                action.source_message_id,
                action.status,
                action.expires_at,
                action.created_at,
                action.consumed_at,
                action.payload_version,
                action.idempotency_key,
                action.source,
                action.source_card_message_id,
                json.dumps(action.result, ensure_ascii=False, sort_keys=True),
                action.created_at,
            ),
        )
        await self.db.commit()

    async def get_pending_action(self, action_id: str) -> PendingAction | None:
        cursor = await self.db.execute(
            "SELECT * FROM pending_actions WHERE action_id = ?",
            (action_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return PendingAction(
            action_id=row["action_id"],
            tenant_key=row["tenant_key"],
            app_id=row["app_id"],
            chat_id=row["chat_id"],
            thread_id=row["thread_id"],
            creator_subject_id=row["creator_subject_id"],
            creator_open_id=row["creator_open_id"],
            action_type=row["action_type"],
            payload=json.loads(row["payload_json"]),
            payload_hash=row["payload_hash"],
            source_message_id=row["source_message_id"],
            status=row["status"],
            expires_at=row["expires_at"],
            created_at=row["created_at"],
            consumed_at=row["consumed_at"],
            attempt_count=row["attempt_count"],
            remote_resource_id=row["remote_resource_id"],
            remote_request_id=row["remote_request_id"],
            payload_version=row["payload_version"],
            idempotency_key=row["idempotency_key"],
            source=row["source"],
            source_card_message_id=row["source_card_message_id"],
            error_code=row["last_error_code"],
            error_message=row["last_error_message"],
            result=json.loads(row["result_json"] or "{}"),
        )

    async def get_pending_action_by_idempotency(
        self,
        *,
        tenant_key: str,
        app_id: str,
        idempotency_key: str,
    ) -> PendingAction | None:
        cursor = await self.db.execute(
            """
            SELECT action_id FROM pending_actions
            WHERE tenant_key = ? AND app_id = ? AND idempotency_key = ?
            """,
            (tenant_key, app_id or self.app_id, idempotency_key),
        )
        row = await cursor.fetchone()
        return await self.get_pending_action(row["action_id"]) if row else None

    async def list_pending_actions(
        self,
        *,
        tenant_key: str,
        app_id: str,
        principal_id: str,
        limit: int = 100,
    ) -> list[PendingAction]:
        cursor = await self.db.execute(
            """
            SELECT action_id FROM pending_actions
            WHERE tenant_key = ? AND app_id = ? AND creator_subject_id = ?
            ORDER BY created_at DESC LIMIT ?
            """,
            (tenant_key, app_id or self.app_id, principal_id, limit),
        )
        actions: list[PendingAction] = []
        for row in await cursor.fetchall():
            action = await self.get_pending_action(row["action_id"])
            if action:
                actions.append(action)
        return actions

    async def set_pending_action_card_message(
        self,
        action_id: str,
        message_id: str,
    ) -> bool:
        cursor = await self.db.execute(
            """
            UPDATE pending_actions
            SET source_card_message_id = ?, updated_at = ?
            WHERE action_id = ?
            """,
            (message_id, int(time.time()), action_id),
        )
        await self.db.commit()
        return cursor.rowcount == 1

    async def ready_outbox_action_ids(self, *, limit: int = 20) -> list[str]:
        """返回可被执行器认领的持久化动作。

        这只是候选读取。`claim_action_execution` 执行权威的原子认领，
        因此多个 worker 可以同时轮询。
        """

        cursor = await self.db.execute(
            """
            SELECT action_id FROM action_outbox
            WHERE status = 'pending' AND available_at <= ?
            ORDER BY available_at, created_at
            LIMIT ?
            """,
            (int(time.time()), limit),
        )
        return [row["action_id"] for row in await cursor.fetchall()]

    async def recover_expired_action_claims(self) -> int:
        """对不确定的执行加栅栏，而不是盲目重试副作用。"""

        now = int(time.time())
        async with self._transaction_lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self.db.execute(
                    """
                    UPDATE pending_actions
                    SET status = 'unknown_remote_state',
                        claimed_by = NULL,
                        claim_expires_at = NULL,
                        last_error_code = 'execution_lease_expired',
                        last_error_message = '执行租约到期，远端结果未知'
                    WHERE status = 'executing' AND claim_expires_at <= ?
                    """,
                    (now,),
                )
                await self.db.execute(
                    """
                    UPDATE action_attempts
                    SET status = 'unknown_remote_state', completed_at = ?,
                        error_code = 'execution_lease_expired',
                        error_message = '执行租约到期，远端结果未知'
                    WHERE status = 'executing'
                      AND action_id IN (
                          SELECT action_id FROM pending_actions
                          WHERE status = 'unknown_remote_state'
                            AND last_error_code = 'execution_lease_expired'
                      )
                    """,
                    (now,),
                )
                await self.db.execute(
                    """
                    UPDATE action_outbox
                    SET status = 'awaiting_reconciliation',
                        claimed_by = NULL, claim_expires_at = NULL
                    WHERE status = 'processing' AND claim_expires_at <= ?
                    """,
                    (now,),
                )
                await self.db.commit()
                return cursor.rowcount
            except Exception:
                await self.db.rollback()
                raise

    async def unknown_remote_actions(self, *, limit: int = 20) -> list[PendingAction]:
        cursor = await self.db.execute(
            """
            SELECT action_id FROM pending_actions
            WHERE status = 'unknown_remote_state'
            ORDER BY created_at
            LIMIT ?
            """,
            (limit,),
        )
        actions: list[PendingAction] = []
        for row in await cursor.fetchall():
            action = await self.get_pending_action(row["action_id"])
            if action is not None:
                actions.append(action)
        return actions

    async def record_action_remote_success(
        self,
        action_id: str,
        *,
        remote_resource_id: str,
        remote_request_id: str = "",
    ) -> bool:
        async with self._transaction_lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self.db.execute(
                    """
                    UPDATE pending_actions
                    SET remote_resource_id = ?, remote_request_id = ?
                    WHERE action_id = ? AND status = 'executing'
                    """,
                    (remote_resource_id, remote_request_id, action_id),
                )
                await self.db.execute(
                    """
                    UPDATE action_attempts
                    SET status = 'remote_succeeded', remote_resource_id = ?,
                        remote_request_id = ?
                    WHERE attempt_id = (
                        SELECT attempt_id FROM action_attempts
                        WHERE action_id = ? AND status = 'executing'
                        ORDER BY attempt_number DESC LIMIT 1
                    )
                    """,
                    (remote_resource_id, remote_request_id, action_id),
                )
                await self.db.commit()
                return cursor.rowcount == 1
            except Exception:
                await self.db.rollback()
                raise

    async def mark_action_unknown(
        self,
        action_id: str,
        *,
        error_code: str,
        error_message: str,
    ) -> bool:
        now = int(time.time())
        async with self._transaction_lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self.db.execute(
                    """
                    UPDATE pending_actions
                    SET status = 'unknown_remote_state', claimed_by = NULL,
                        claim_expires_at = NULL, last_error_code = ?,
                        last_error_message = ?
                    WHERE action_id = ? AND status = 'executing'
                    """,
                    (error_code, error_message[:500], action_id),
                )
                await self.db.execute(
                    """
                    UPDATE action_attempts
                    SET status = 'unknown_remote_state', completed_at = ?,
                        error_code = ?, error_message = ?
                    WHERE attempt_id = (
                        SELECT attempt_id FROM action_attempts
                        WHERE action_id = ? AND status IN ('executing', 'remote_succeeded')
                        ORDER BY attempt_number DESC LIMIT 1
                    )
                    """,
                    (now, error_code, error_message[:500], action_id),
                )
                await self.db.execute(
                    """
                    UPDATE action_outbox
                    SET status = 'awaiting_reconciliation', claimed_by = NULL,
                        claim_expires_at = NULL
                    WHERE action_id = ? AND status = 'processing'
                    """,
                    (action_id,),
                )
                await self.db.commit()
                return cursor.rowcount == 1
            except Exception:
                await self.db.rollback()
                raise

    async def finish_reconciled_action(self, action_id: str) -> bool:
        now = int(time.time())
        async with self._transaction_lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self.db.execute(
                    """
                    UPDATE pending_actions
                    SET status = 'succeeded', completed_at = ?,
                        last_error_code = '', last_error_message = ''
                    WHERE action_id = ? AND status = 'unknown_remote_state'
                    """,
                    (now, action_id),
                )
                await self.db.execute(
                    """
                    UPDATE action_outbox
                    SET status = 'processed', processed_at = ?
                    WHERE action_id = ? AND status = 'awaiting_reconciliation'
                    """,
                    (now, action_id),
                )
                await self.db.commit()
                return cursor.rowcount == 1
            except Exception:
                await self.db.rollback()
                raise

    async def claim_pending_action(
        self,
        action_id: str,
        *,
        actor_open_id: str,
        payload_hash: str,
    ) -> bool:
        now = int(time.time())
        async with self._transaction_lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self.db.execute(
                    """
                    UPDATE pending_actions
                    SET status = 'confirmed', confirmed_at = ?, consumed_at = ?
                    WHERE action_id = ?
                      AND creator_open_id = ?
                      AND payload_hash = ?
                      AND status IN ('awaiting_confirmation', 'failed_retryable')
                      AND expires_at > ?
                    """,
                    (now, now, action_id, actor_open_id, payload_hash, now),
                )
                if cursor.rowcount == 1:
                    action_row = await (
                        await self.db.execute(
                            "SELECT action_type FROM pending_actions WHERE action_id = ?",
                            (action_id,),
                        )
                    ).fetchone()
                    await self.db.execute(
                        """
                        INSERT INTO action_outbox(
                            outbox_id, action_id, event_type, payload_json, status,
                            available_at, attempt_count, created_at
                        ) VALUES (?, ?, ?, '{}', 'pending', ?, 0, ?)
                        """,
                        (
                            str(uuid.uuid4()),
                            action_id,
                            f"{action_row['action_type']}.requested",
                            now,
                            now,
                        ),
                    )
                await self.db.commit()
                return cursor.rowcount == 1
            except Exception:
                await self.db.rollback()
                raise

    async def retry_pending_action(
        self,
        action_id: str,
        *,
        tenant_key: str,
        app_id: str,
        principal_id: str,
    ) -> bool:
        now = int(time.time())
        async with self._transaction_lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self.db.execute(
                    """
                    UPDATE pending_actions
                    SET status = 'confirmed', updated_at = ?,
                        last_error_code = '', last_error_message = ''
                    WHERE action_id = ? AND tenant_key = ? AND app_id = ?
                      AND creator_subject_id = ? AND status = 'failed_retryable'
                      AND expires_at > ?
                    """,
                    (now, action_id, tenant_key, app_id or self.app_id, principal_id, now),
                )
                if cursor.rowcount == 1:
                    action_row = await (
                        await self.db.execute(
                            "SELECT action_type FROM pending_actions WHERE action_id = ?",
                            (action_id,),
                        )
                    ).fetchone()
                    await self.db.execute(
                        """
                        INSERT INTO action_outbox(
                            outbox_id, action_id, event_type, payload_json, status,
                            available_at, attempt_count, created_at
                        ) VALUES (?, ?, ?, '{}', 'pending', ?, 0, ?)
                        """,
                        (
                            str(uuid.uuid4()),
                            action_id,
                            f"{action_row['action_type']}.requested",
                            now,
                            now,
                        ),
                    )
                await self.db.commit()
                return cursor.rowcount == 1
            except Exception:
                await self.db.rollback()
                raise

    async def claim_action_execution(
        self,
        action_id: str,
        *,
        worker_id: str,
        lease_seconds: int = 60,
    ) -> bool:
        now = int(time.time())
        async with self._transaction_lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self.db.execute(
                    """
                    UPDATE pending_actions
                    SET status = 'executing', claimed_by = ?, claim_expires_at = ?,
                        attempt_count = attempt_count + 1
                    WHERE action_id = ? AND status = 'confirmed' AND expires_at > ?
                    """,
                    (worker_id, now + lease_seconds, action_id, now),
                )
                if cursor.rowcount == 1:
                    row = await (
                        await self.db.execute(
                            "SELECT attempt_count, payload_hash FROM pending_actions "
                            "WHERE action_id = ?",
                            (action_id,),
                        )
                    ).fetchone()
                    await self.db.execute(
                        """
                        INSERT INTO action_attempts(
                            attempt_id, action_id, attempt_number, started_at,
                            status, request_fingerprint
                        ) VALUES (?, ?, ?, ?, 'executing', ?)
                        """,
                        (
                            str(uuid.uuid4()),
                            action_id,
                            row["attempt_count"],
                            now,
                            row["payload_hash"],
                        ),
                    )
                    await self.db.execute(
                        """
                        UPDATE action_outbox
                        SET status = 'processing', claimed_by = ?, claim_expires_at = ?,
                            attempt_count = attempt_count + 1
                        WHERE outbox_id = (
                            SELECT outbox_id FROM action_outbox
                            WHERE action_id = ? AND status = 'pending'
                            ORDER BY created_at LIMIT 1
                        )
                        """,
                        (worker_id, now + lease_seconds, action_id),
                    )
                await self.db.commit()
                return cursor.rowcount == 1
            except Exception:
                await self.db.rollback()
                raise

    async def finish_pending_action(
        self,
        action_id: str,
        *,
        success: bool,
    ) -> bool:
        now = int(time.time())
        status = "succeeded" if success else "failed_retryable"
        async with self._transaction_lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self.db.execute(
                    """
                    UPDATE pending_actions
                    SET status = ?, completed_at = CASE WHEN ? THEN ? ELSE NULL END,
                        claimed_by = NULL, claim_expires_at = NULL
                    WHERE action_id = ? AND status = 'executing'
                    """,
                    (status, success, now, action_id),
                )
                await self.db.execute(
                    """
                    UPDATE action_attempts
                    SET status = ?, completed_at = ?
                    WHERE attempt_id = (
                        SELECT attempt_id FROM action_attempts
                        WHERE action_id = ? AND status IN ('executing', 'remote_succeeded')
                        ORDER BY attempt_number DESC LIMIT 1
                    )
                    """,
                    (status, now, action_id),
                )
                await self.db.execute(
                    """
                    UPDATE action_outbox
                    SET status = ?, processed_at = ?, claimed_by = NULL,
                        claim_expires_at = NULL
                    WHERE outbox_id = (
                        SELECT outbox_id FROM action_outbox
                        WHERE action_id = ? AND status = 'processing'
                        ORDER BY created_at DESC LIMIT 1
                    )
                    """,
                    ("processed" if success else "failed", now, action_id),
                )
                await self.db.commit()
                return cursor.rowcount == 1
            except Exception:
                await self.db.rollback()
                raise

    async def complete_pending_action(
        self,
        action_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error_code: str = "",
        error_message: str = "",
        remote_resource_id: str = "",
        remote_request_id: str = "",
    ) -> bool:
        if status not in {"succeeded", "failed_retryable", "failed_final"}:
            raise ValueError(f"unsupported terminal status: {status}")
        now = int(time.time())
        result_json = json.dumps(
            result or {},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        async with self._transaction_lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self.db.execute(
                    """
                    UPDATE pending_actions
                    SET status = ?, completed_at = CASE
                            WHEN ? IN ('succeeded', 'failed_final') THEN ? ELSE NULL END,
                        claimed_by = NULL, claim_expires_at = NULL,
                        result_json = ?, last_error_code = ?, last_error_message = ?,
                        remote_resource_id = ?, remote_request_id = ?, updated_at = ?
                    WHERE action_id = ? AND status = 'executing'
                    """,
                    (
                        status,
                        status,
                        now,
                        result_json,
                        error_code,
                        error_message[:500],
                        remote_resource_id,
                        remote_request_id,
                        now,
                        action_id,
                    ),
                )
                await self.db.execute(
                    """
                    UPDATE action_attempts
                    SET status = ?, completed_at = ?, error_code = ?, error_message = ?,
                        remote_resource_id = ?, remote_request_id = ?
                    WHERE attempt_id = (
                        SELECT attempt_id FROM action_attempts
                        WHERE action_id = ? AND status IN ('executing', 'remote_succeeded')
                        ORDER BY attempt_number DESC LIMIT 1
                    )
                    """,
                    (
                        status,
                        now,
                        error_code,
                        error_message[:500],
                        remote_resource_id,
                        remote_request_id,
                        action_id,
                    ),
                )
                await self.db.execute(
                    """
                    UPDATE action_outbox
                    SET status = ?, processed_at = ?, claimed_by = NULL,
                        claim_expires_at = NULL
                    WHERE outbox_id = (
                        SELECT outbox_id FROM action_outbox
                        WHERE action_id = ? AND status = 'processing'
                        ORDER BY created_at DESC LIMIT 1
                    )
                    """,
                    (
                        "processed" if status == "succeeded" else "failed",
                        now,
                        action_id,
                    ),
                )
                await self.db.commit()
                return cursor.rowcount == 1
            except Exception:
                await self.db.rollback()
                raise

    async def cancel_pending_action(
        self,
        action_id: str,
        *,
        actor_open_id: str,
        payload_hash: str,
    ) -> bool:
        now = int(time.time())
        cursor = await self.db.execute(
            """
            UPDATE pending_actions
            SET status = 'cancelled', consumed_at = ?
            WHERE action_id = ?
              AND creator_open_id = ?
              AND payload_hash = ?
              AND status IN ('awaiting_confirmation', 'failed_retryable')
              AND expires_at > ?
            """,
            (now, action_id, actor_open_id, payload_hash, now),
        )
        await self.db.commit()
        return cursor.rowcount == 1

    async def expire_pending_action(self, action_id: str) -> bool:
        cursor = await self.db.execute(
            """
            UPDATE pending_actions SET status = 'expired', consumed_at = ?
            WHERE action_id = ? AND status IN (
                'awaiting_confirmation', 'confirmed', 'executing', 'failed_retryable'
            )
            """,
            (int(time.time()), action_id),
        )
        await self.db.commit()
        return cursor.rowcount == 1

    async def _import_legacy_json_once(self) -> None:
        if not self.legacy_json_path or not self.legacy_json_path.is_file():
            return
        cursor = await self.db.execute(
            "SELECT value FROM metadata WHERE key = 'legacy_json_sha256'"
        )
        if await cursor.fetchone():
            return
        raw = self.legacy_json_path.read_bytes()
        payload = json.loads(raw)
        state = _normalize_legacy_state(payload)
        await self.db.execute("BEGIN IMMEDIATE")
        try:
            for value in state["records"].values():
                try:
                    record = DailyRecord.model_validate(value)
                except Exception:
                    continue
                if not record.app_id:
                    record.app_id = self.app_id
                if record.key.count(":") == 2:
                    record.key = self.record_key(record.chat_id, record.user_id, record.date)
                await self.save_record_without_commit(record)
            for user_id, value in state["auth"].items():
                try:
                    token = OAuthToken.model_validate({"user_id": user_id, **value})
                except Exception:
                    continue
                token.app_id = token.app_id or self.app_id
                await self.save_token_without_commit(token)
            for event_id in state["processed_message_ids"]:
                await self.db.execute(
                    """
                    INSERT INTO processed_events(event_id, event_type, processed_at)
                    VALUES (?, 'message', ?) ON CONFLICT DO NOTHING
                    """,
                    (event_id, int(time.time())),
                )
            for chat_id in state["group_chat_ids"]:
                await self.db.execute(
                    """
                    INSERT INTO group_chats(tenant_key, app_id, chat_id, created_at)
                    VALUES ('', ?, ?, ?) ON CONFLICT DO NOTHING
                    """,
                    (self.app_id, chat_id, int(time.time())),
                )
            for user_id, chat_id in state["p2p_chat_ids"].items():
                await self.db.execute(
                    """
                    INSERT INTO p2p_chats(tenant_key, app_id, subject_id, chat_id, updated_at)
                    VALUES ('', ?, ?, ?, ?) ON CONFLICT DO NOTHING
                    """,
                    (self.app_id, user_id, chat_id, int(time.time())),
                )
            for value in state["document_bindings"].values():
                try:
                    binding = DocumentBinding.model_validate(value)
                except Exception:
                    continue
                await self.save_document_binding_without_commit(binding)
            await self.db.execute(
                "INSERT INTO metadata(key, value) VALUES ('legacy_json_sha256', ?)",
                (hashlib.sha256(raw).hexdigest(),),
            )
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            raise

    async def save_record_without_commit(self, record: DailyRecord) -> None:
        await self.db.execute(
            """
            INSERT INTO daily_plans(
                record_key, tenant_key, app_id, chat_id, thread_id, subject_id,
                plan_date, payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                record.key,
                record.tenant_key,
                record.app_id or self.app_id,
                record.chat_id,
                record.thread_id,
                record.user_id,
                record.date,
                record.model_dump_json(),
                record.created_at,
                record.updated_at,
            ),
        )

    async def save_token_without_commit(self, token: OAuthToken) -> None:
        access, refresh = self._encrypt_token(token)
        await self.db.execute(
            """
            INSERT INTO oauth_tokens(
                tenant_key, app_id, subject_id, open_id, tenant_user_id, union_id,
                access_token, refresh_token,
                access_token_ciphertext, access_token_nonce,
                refresh_token_ciphertext, refresh_token_nonce, encryption_key_version,
                expires_at, refresh_expires_at, scope, updated_at, disabled_at
            ) VALUES (?, ?, ?, ?, ?, ?, '', '', ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT DO NOTHING
            """,
            (
                token.tenant_key,
                token.app_id or self.app_id,
                token.user_id,
                token.open_id,
                token.tenant_user_id,
                token.union_id,
                access.ciphertext,
                access.nonce,
                refresh.ciphertext,
                refresh.nonce,
                access.key_version,
                token.expires_at,
                token.refresh_expires_at,
                token.scope,
                int(time.time()),
            ),
        )

    def _encrypt_token(self, token: OAuthToken) -> tuple[EncryptedValue, EncryptedValue]:
        tenant_key = token.tenant_key
        app_id = token.app_id or self.app_id
        principal_id = token.user_id
        access = self.token_cipher.encrypt(
            token.access_token.encode(),
            associated_data=token_associated_data(
                tenant_key, app_id, principal_id, "access"
            ),
        )
        refresh = self.token_cipher.encrypt(
            token.refresh_token.encode(),
            associated_data=token_associated_data(
                tenant_key, app_id, principal_id, "refresh"
            ),
        )
        return access, refresh

    def _token_from_row(self, row: aiosqlite.Row) -> OAuthToken:
        key_version = row["encryption_key_version"]
        if key_version is None:
            raise TokenCipherError("OAuth credential has not been encrypted")
        access = self.token_cipher.decrypt(
            EncryptedValue(
                ciphertext=row["access_token_ciphertext"] or b"",
                nonce=row["access_token_nonce"] or b"",
                key_version=key_version,
            ),
            associated_data=token_associated_data(
                row["tenant_key"], row["app_id"], row["subject_id"], "access"
            ),
        )
        refresh = self.token_cipher.decrypt(
            EncryptedValue(
                ciphertext=row["refresh_token_ciphertext"] or b"",
                nonce=row["refresh_token_nonce"] or b"",
                key_version=key_version,
            ),
            associated_data=token_associated_data(
                row["tenant_key"], row["app_id"], row["subject_id"], "refresh"
            ),
        )
        return OAuthToken(
            user_id=row["subject_id"],
            tenant_key=row["tenant_key"],
            app_id=row["app_id"],
            open_id=row["open_id"],
            tenant_user_id=row["tenant_user_id"],
            union_id=row["union_id"],
            access_token=access.decode(),
            refresh_token=refresh.decode(),
            expires_at=row["expires_at"],
            refresh_expires_at=row["refresh_expires_at"],
            scope=row["scope"],
        )

    async def save_document_binding_without_commit(self, binding: DocumentBinding) -> None:
        await self.db.execute(
            """
            INSERT INTO document_bindings(
                tenant_key, app_id, chat_id, thread_id, subject_id,
                title, token, url, updated_at
            ) VALUES ('', ?, ?, '', ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                self.app_id,
                binding.chat_id,
                binding.user_id,
                binding.title,
                binding.token,
                binding.url,
                int(time.time()),
            ),
        )

    async def append_conversation_message(
        self,
        *,
        session_id: str,
        message_id: str,
        role: str,
        content: str,
        tenant_key: str,
        app_id: str,
        principal_id: str,
        chat_id: str = "",
        thread_id: str = "",
    ) -> bool:
        cursor = await self.db.execute(
            """
            INSERT INTO conversation_messages(
                row_id, session_id, tenant_key, app_id, principal_id,
                chat_id, thread_id, message_id, role, content, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tenant_key, app_id, principal_id, message_id) DO NOTHING
            """,
            (
                str(uuid.uuid4()),
                session_id,
                tenant_key,
                app_id or self.app_id,
                principal_id,
                chat_id,
                thread_id,
                message_id,
                role,
                content,
                int(time.time()),
            ),
        )
        await self.db.commit()
        return cursor.rowcount == 1

    async def list_recent_conversation_messages(
        self,
        session_id: str,
        *,
        tenant_key: str,
        app_id: str,
        principal_id: str,
        limit: int = 8,
    ):
        from .context.models import ConversationMessage

        cursor = await self.db.execute(
            """
            SELECT message_id, role, content, created_at
            FROM conversation_messages
            WHERE tenant_key = ? AND app_id = ? AND principal_id = ?
              AND session_id = ?
            ORDER BY created_at DESC, rowid DESC
            LIMIT ?
            """,
            (tenant_key, app_id or self.app_id, principal_id, session_id, limit),
        )
        rows = list(reversed(await cursor.fetchall()))
        return [ConversationMessage.model_validate(dict(row)) for row in rows]

    async def list_conversation_messages_for_compaction(
        self,
        session_id: str,
        *,
        tenant_key: str,
        app_id: str,
        principal_id: str,
        keep_recent: int,
        threshold: int,
    ):
        from .context.models import ConversationMessage

        count_row = await (
            await self.db.execute(
                """
                SELECT COUNT(*) AS count
                FROM conversation_messages
                WHERE tenant_key = ? AND app_id = ? AND principal_id = ?
                  AND session_id = ? AND summarized_at IS NULL
                """,
                (tenant_key, app_id or self.app_id, principal_id, session_id),
            )
        ).fetchone()
        if not count_row or count_row["count"] < threshold:
            return []
        cursor = await self.db.execute(
            """
            SELECT message_id, role, content, created_at
            FROM conversation_messages
            WHERE tenant_key = ? AND app_id = ? AND principal_id = ?
              AND session_id = ? AND summarized_at IS NULL
            ORDER BY created_at, rowid
            LIMIT ?
            """,
            (
                tenant_key,
                app_id or self.app_id,
                principal_id,
                session_id,
                max(0, count_row["count"] - keep_recent),
            ),
        )
        return [
            ConversationMessage.model_validate(dict(row))
            for row in await cursor.fetchall()
        ]

    async def get_conversation_summary(
        self,
        session_id: str,
        *,
        tenant_key: str,
        app_id: str,
        principal_id: str,
    ):
        from .context.models import ConversationSummary

        row = await (
            await self.db.execute(
                """
                SELECT summary_json
                FROM conversation_summaries
                WHERE tenant_key = ? AND app_id = ? AND principal_id = ?
                  AND session_id = ?
                """,
                (tenant_key, app_id or self.app_id, principal_id, session_id),
            )
        ).fetchone()
        return (
            ConversationSummary.model_validate_json(row["summary_json"])
            if row
            else None
        )

    async def save_conversation_summary(
        self,
        session_id: str,
        summary,
        *,
        covered_message_ids: list[str],
        tenant_key: str,
        app_id: str,
        principal_id: str,
    ) -> None:
        now = int(time.time())
        async with self._transaction_lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                await self.db.execute(
                    """
                    INSERT INTO conversation_summaries(
                        summary_id, session_id, tenant_key, app_id, principal_id,
                        summary_json, covered_message_count, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(tenant_key, app_id, principal_id, session_id)
                    DO UPDATE SET
                        summary_json = excluded.summary_json,
                        covered_message_count =
                            conversation_summaries.covered_message_count
                            + excluded.covered_message_count,
                        updated_at = excluded.updated_at
                    """,
                    (
                        str(uuid.uuid4()),
                        session_id,
                        tenant_key,
                        app_id or self.app_id,
                        principal_id,
                        summary.model_dump_json(),
                        len(covered_message_ids),
                        now,
                        now,
                    ),
                )
                if covered_message_ids:
                    placeholders = ",".join("?" for _ in covered_message_ids)
                    await self.db.execute(
                        f"""
                        UPDATE conversation_messages
                        SET summarized_at = ?
                        WHERE tenant_key = ? AND app_id = ? AND principal_id = ?
                          AND session_id = ? AND message_id IN ({placeholders})
                        """,
                        (
                            now,
                            tenant_key,
                            app_id or self.app_id,
                            principal_id,
                            session_id,
                            *covered_message_ids,
                        ),
                    )
                await self.db.commit()
            except Exception:
                await self.db.rollback()
                raise

    async def upsert_user_memory(
        self,
        memory,
        *,
        tenant_key: str,
        app_id: str,
        principal_id: str,
    ) -> None:
        from datetime import UTC, datetime

        now = datetime.now(UTC).isoformat()
        await self.db.execute(
            """
            INSERT INTO user_memories(
                memory_id, tenant_key, app_id, principal_id,
                memory_type, memory_key, memory_value, confidence,
                source_message_id, valid_from, valid_until,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(
                tenant_key, app_id, principal_id, memory_type, memory_key
            ) DO UPDATE SET
                memory_value = excluded.memory_value,
                confidence = excluded.confidence,
                source_message_id = excluded.source_message_id,
                valid_until = excluded.valid_until,
                updated_at = excluded.updated_at
            """,
            (
                str(uuid.uuid4()),
                tenant_key,
                app_id or self.app_id,
                principal_id,
                memory.memory_type,
                memory.memory_key,
                memory.memory_value,
                memory.confidence,
                memory.source_message_id,
                now,
                memory.valid_until,
                now,
                now,
            ),
        )
        await self.db.commit()

    async def list_user_memories(
        self,
        *,
        tenant_key: str,
        app_id: str,
        principal_id: str,
        keys: list[str] | None = None,
        limit: int = 20,
    ):
        from .context.models import MemoryFact

        query = """
            SELECT memory_type, memory_key, memory_value, confidence,
                   source_message_id, valid_until
            FROM user_memories
            WHERE tenant_key = ? AND app_id = ? AND principal_id = ?
              AND (valid_until = '' OR valid_until > ?)
        """
        from datetime import UTC, datetime

        params: list[Any] = [
            tenant_key,
            app_id or self.app_id,
            principal_id,
            datetime.now(UTC).isoformat(),
        ]
        if keys:
            query += f" AND memory_key IN ({','.join('?' for _ in keys)})"
            params.extend(keys)
        query += " ORDER BY confidence DESC, updated_at DESC LIMIT ?"
        params.append(limit)
        cursor = await self.db.execute(query, params)
        return [MemoryFact.model_validate(dict(row)) for row in await cursor.fetchall()]

    async def find_active_pending_action(
        self,
        *,
        tenant_key: str,
        app_id: str,
        principal_id: str,
        thread_id: str = "",
    ) -> dict[str, Any] | None:
        row = await (
            await self.db.execute(
                """
                SELECT action_id, action_type, status, expires_at, payload_json
                FROM pending_actions
                WHERE tenant_key = ? AND app_id = ? AND creator_subject_id = ?
                  AND thread_id = ?
                  AND status IN (
                    'awaiting_confirmation', 'confirmed', 'executing',
                    'failed_retryable'
                  )
                ORDER BY created_at DESC LIMIT 1
                """,
                (
                    tenant_key,
                    app_id or self.app_id,
                    principal_id,
                    thread_id,
                ),
            )
        ).fetchone()
        if not row:
            return None
        return {
            "action_id": row["action_id"],
            "action_type": row["action_type"],
            "status": row["status"],
            "expires_at": row["expires_at"],
            "payload": json.loads(row["payload_json"]),
        }

    async def save_tool_result(
        self,
        *,
        tenant_key: str,
        app_id: str,
        principal_id: str,
        tool_name: str,
        summary: str,
        payload: dict[str, Any],
        truncated: bool,
        expires_at: int | None = None,
    ) -> str:
        result_ref = f"tool_result_{uuid.uuid4().hex}"
        await self.db.execute(
            """
            INSERT INTO tool_results(
                result_ref, tenant_key, app_id, principal_id, tool_name,
                summary, payload_json, truncated, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result_ref,
                tenant_key,
                app_id or self.app_id,
                principal_id,
                tool_name,
                summary,
                json.dumps(payload, ensure_ascii=False),
                int(truncated),
                int(time.time()),
                expires_at,
            ),
        )
        await self.db.commit()
        return result_ref

    async def get_tool_result(
        self,
        result_ref: str,
        *,
        tenant_key: str,
        app_id: str,
        principal_id: str,
    ) -> dict[str, Any] | None:
        row = await (
            await self.db.execute(
                """
                SELECT tool_name, summary, payload_json, truncated, created_at
                FROM tool_results
                WHERE result_ref = ? AND tenant_key = ? AND app_id = ?
                  AND principal_id = ?
                  AND (expires_at IS NULL OR expires_at > ?)
                """,
                (
                    result_ref,
                    tenant_key,
                    app_id or self.app_id,
                    principal_id,
                    int(time.time()),
                ),
            )
        ).fetchone()
        if not row:
            return None
        return {
            "result_ref": result_ref,
            "tool_name": row["tool_name"],
            "summary": row["summary"],
            "payload": json.loads(row["payload_json"]),
            "truncated": bool(row["truncated"]),
            "created_at": row["created_at"],
        }

    async def reserve_api_request(
        self,
        *,
        tenant_key: str,
        principal_id: str,
        idempotency_key: str,
        request_hash: str,
        ttl_seconds: int,
    ) -> tuple[str, dict[str, Any] | None]:
        now = int(time.time())
        async with self._transaction_lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await self.db.execute(
                        """
                        SELECT request_hash, status, response_json, expires_at
                        FROM api_idempotency
                        WHERE tenant_key = ? AND principal_id = ? AND idempotency_key = ?
                        """,
                        (tenant_key, principal_id, idempotency_key),
                    )
                ).fetchone()
                if row and row["expires_at"] <= now:
                    await self.db.execute(
                        """
                        DELETE FROM api_idempotency
                        WHERE tenant_key = ? AND principal_id = ? AND idempotency_key = ?
                        """,
                        (tenant_key, principal_id, idempotency_key),
                    )
                    row = None
                if row:
                    await self.db.commit()
                    if row["request_hash"] != request_hash:
                        return "conflict", None
                    if row["status"] == "completed":
                        return "replay", json.loads(row["response_json"])
                    return "in_progress", None
                await self.db.execute(
                    """
                    INSERT INTO api_idempotency(
                        tenant_key, principal_id, idempotency_key, request_hash,
                        status, response_json, created_at, expires_at
                    ) VALUES (?, ?, ?, ?, 'processing', '', ?, ?)
                    """,
                    (
                        tenant_key,
                        principal_id,
                        idempotency_key,
                        request_hash,
                        now,
                        now + ttl_seconds,
                    ),
                )
                await self.db.commit()
                return "reserved", None
            except Exception:
                await self.db.rollback()
                raise

    async def complete_api_request(
        self,
        *,
        tenant_key: str,
        principal_id: str,
        idempotency_key: str,
        response: dict[str, Any],
    ) -> None:
        await self.db.execute(
            """
            UPDATE api_idempotency SET status = 'completed', response_json = ?
            WHERE tenant_key = ? AND principal_id = ? AND idempotency_key = ?
              AND status = 'processing'
            """,
            (
                json.dumps(response, ensure_ascii=False, separators=(",", ":")),
                tenant_key,
                principal_id,
                idempotency_key,
            ),
        )
        await self.db.commit()

    async def abandon_api_request(
        self,
        *,
        tenant_key: str,
        principal_id: str,
        idempotency_key: str,
    ) -> None:
        await self.db.execute(
            """
            DELETE FROM api_idempotency
            WHERE tenant_key = ? AND principal_id = ? AND idempotency_key = ?
              AND status = 'processing'
            """,
            (tenant_key, principal_id, idempotency_key),
        )
        await self.db.commit()

    async def create_api_binding_code(
        self,
        *,
        code_hash: str,
        tenant_key: str,
        app_id: str,
        principal_id: str,
        ttl_seconds: int,
    ) -> int:
        now = int(time.time())
        expires_at = now + ttl_seconds
        await self.db.execute(
            """
            INSERT INTO api_binding_codes(
                code_hash, tenant_key, app_id, principal_id,
                created_at, expires_at, consumed_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULL)
            """,
            (code_hash, tenant_key, app_id, principal_id, now, expires_at),
        )
        await self.db.commit()
        return expires_at

    async def redeem_api_binding_code(
        self,
        *,
        code_hash: str,
        identity: FeishuIdentity,
        chat_id: str,
    ) -> ApiChannelBinding | None:
        now = int(time.time())
        async with self._transaction_lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await self.db.execute(
                        """
                        SELECT tenant_key, app_id, principal_id
                        FROM api_binding_codes
                        WHERE code_hash = ? AND consumed_at IS NULL AND expires_at > ?
                        """,
                        (code_hash, now),
                    )
                ).fetchone()
                if not row:
                    await self.db.commit()
                    return None
                binding = ApiChannelBinding(
                    binding_id=f"binding_{uuid.uuid4().hex}",
                    tenant_key=row["tenant_key"],
                    app_id=row["app_id"],
                    principal_id=row["principal_id"],
                    provider="feishu",
                    external_tenant_key=identity.tenant_key,
                    external_app_id=identity.app_id,
                    external_subject_id=identity.subject_id,
                    external_open_id=identity.open_id,
                    external_user_id=identity.user_id,
                    external_union_id=identity.union_id,
                    external_chat_id=chat_id,
                    created_at=now,
                    updated_at=now,
                )
                await self.db.execute(
                    "UPDATE api_binding_codes SET consumed_at = ? WHERE code_hash = ?",
                    (now, code_hash),
                )
                await self.db.execute(
                    """
                    INSERT INTO api_channel_bindings(
                        binding_id, tenant_key, app_id, principal_id, provider,
                        external_tenant_key, external_app_id, external_subject_id,
                        external_open_id, external_user_id, external_union_id,
                        external_chat_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    tuple(binding.model_dump().values()),
                )
                await self.db.commit()
                return binding
            except Exception:
                await self.db.rollback()
                raise

    async def get_api_channel_binding(
        self,
        binding_id: str,
        *,
        tenant_key: str,
        app_id: str,
        principal_id: str,
    ) -> ApiChannelBinding | None:
        row = await (
            await self.db.execute(
                """
                SELECT * FROM api_channel_bindings
                WHERE binding_id = ? AND tenant_key = ? AND app_id = ? AND principal_id = ?
                """,
                (binding_id, tenant_key, app_id, principal_id),
            )
        ).fetchone()
        return ApiChannelBinding.model_validate(dict(row)) if row else None

    async def list_api_channel_bindings(
        self,
        *,
        tenant_key: str,
        app_id: str,
        principal_id: str,
    ) -> list[ApiChannelBinding]:
        rows = await (
            await self.db.execute(
                """
                SELECT * FROM api_channel_bindings
                WHERE tenant_key = ? AND app_id = ? AND principal_id = ?
                ORDER BY created_at, binding_id
                """,
                (tenant_key, app_id, principal_id),
            )
        ).fetchall()
        return [ApiChannelBinding.model_validate(dict(row)) for row in rows]

    async def delete_api_channel_binding(
        self,
        binding_id: str,
        *,
        tenant_key: str,
        app_id: str,
        principal_id: str,
    ) -> bool:
        cursor = await self.db.execute(
            """
            DELETE FROM api_channel_bindings
            WHERE binding_id = ? AND tenant_key = ? AND app_id = ? AND principal_id = ?
            """,
            (binding_id, tenant_key, app_id, principal_id),
        )
        await self.db.commit()
        return cursor.rowcount == 1


def _normalize_legacy_state(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("version") == 2:
        return {
            "records": value.get("records", {}),
            "auth": value.get("auth", {}),
            "processed_message_ids": value.get("processed_message_ids", []),
            "p2p_chat_ids": value.get("p2p_chat_ids", {}),
            "group_chat_ids": value.get("group_chat_ids", []),
            "document_bindings": value.get("document_bindings", {}),
        }
    state: dict[str, Any] = {
        "records": {},
        "auth": {},
        "processed_message_ids": value.get("processedMessageIds", []),
        "p2p_chat_ids": {},
        "group_chat_ids": value.get("groupChatIds", []),
        "document_bindings": {},
    }
    binding = value.get("binding") or {}
    if binding.get("userId") and binding.get("chatId"):
        state["p2p_chat_ids"][binding["userId"]] = binding["chatId"]
    for user_id, token in value.get("auth", {}).items():
        state["auth"][user_id] = {
            "access_token": token.get("accessToken", ""),
            "refresh_token": token.get("refreshToken", ""),
            "expires_at": token.get("expiresAt", 0),
            "refresh_expires_at": token.get("refreshExpiresAt", 0),
            "scope": token.get("scope", ""),
        }
    return state


def _secret_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()
