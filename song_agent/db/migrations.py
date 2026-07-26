"""有序的 SQLite Schema 迁移。"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

import aiosqlite

from ..services.encryption import AesGcmTokenCipher, token_associated_data

Migration = Callable[[aiosqlite.Connection, AesGcmTokenCipher], Awaitable[None]]


async def run_migrations(
    db: aiosqlite.Connection,
    cipher: AesGcmTokenCipher,
) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            migration_id TEXT PRIMARY KEY,
            applied_at INTEGER NOT NULL
        )
        """
    )
    migrations: tuple[tuple[str, Migration], ...] = (
        ("0001_encrypt_oauth_tokens", _encrypt_oauth_tokens),
        ("0002_reliable_pending_actions", _reliable_pending_actions),
        ("0003_scheduler_lease", _scheduler_lease),
        ("0004_audit_log_v2", _audit_log_v2),
        ("0005_oauth_refresh_lease", _oauth_refresh_lease),
        ("0006_scheduled_jobs_v2", _scheduled_jobs_v2),
        ("0007_agent_history", _agent_history),
        ("0008_oauth_original_request", _oauth_original_request),
        ("0009_business_pending_actions", _business_pending_actions),
        ("0010_context_memory", _context_memory),
    )
    for migration_id, migration in migrations:
        cursor = await db.execute(
            "SELECT 1 FROM schema_migrations WHERE migration_id = ?",
            (migration_id,),
        )
        if await cursor.fetchone():
            continue
        await db.execute("BEGIN IMMEDIATE")
        try:
            await migration(db, cipher)
            await db.execute(
                "INSERT INTO schema_migrations(migration_id, applied_at) VALUES (?, ?)",
                (migration_id, int(time.time())),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise


async def rotate_oauth_tokens(
    db: aiosqlite.Connection,
    cipher: AesGcmTokenCipher,
) -> int:
    cursor = await db.execute(
        """
        SELECT tenant_key, app_id, subject_id,
               access_token_ciphertext, access_token_nonce,
               refresh_token_ciphertext, refresh_token_nonce,
               encryption_key_version
        FROM oauth_tokens
        WHERE disabled_at IS NULL
          AND encryption_key_version IS NOT NULL
          AND encryption_key_version != ?
        """,
        (cipher.active_key_version,),
    )
    rows = await cursor.fetchall()
    rotated = 0
    await db.execute("BEGIN IMMEDIATE")
    try:
        for row in rows:
            access = cipher.rotate(
                _encrypted(row, "access"),
                associated_data=token_associated_data(
                    row["tenant_key"], row["app_id"], row["subject_id"], "access"
                ),
            )
            refresh = cipher.rotate(
                _encrypted(row, "refresh"),
                associated_data=token_associated_data(
                    row["tenant_key"], row["app_id"], row["subject_id"], "refresh"
                ),
            )
            await db.execute(
                """
                UPDATE oauth_tokens
                SET access_token_ciphertext = ?, access_token_nonce = ?,
                    refresh_token_ciphertext = ?, refresh_token_nonce = ?,
                    encryption_key_version = ?, updated_at = ?
                WHERE tenant_key = ? AND app_id = ? AND subject_id = ?
                """,
                (
                    access.ciphertext,
                    access.nonce,
                    refresh.ciphertext,
                    refresh.nonce,
                    access.key_version,
                    int(time.time()),
                    row["tenant_key"],
                    row["app_id"],
                    row["subject_id"],
                ),
            )
            rotated += 1
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return rotated


async def _encrypt_oauth_tokens(
    db: aiosqlite.Connection,
    cipher: AesGcmTokenCipher,
) -> None:
    columns = await _columns(db, "oauth_tokens")
    additions = {
        "access_token_ciphertext": "BLOB",
        "access_token_nonce": "BLOB",
        "refresh_token_ciphertext": "BLOB",
        "refresh_token_nonce": "BLOB",
        "encryption_key_version": "INTEGER",
        "disabled_at": "INTEGER",
    }
    for name, column_type in additions.items():
        if name not in columns:
            await db.execute(f"ALTER TABLE oauth_tokens ADD COLUMN {name} {column_type}")

    cursor = await db.execute(
        """
        SELECT tenant_key, app_id, subject_id, access_token, refresh_token
        FROM oauth_tokens
        WHERE access_token_ciphertext IS NULL AND access_token != ''
        """
    )
    for row in await cursor.fetchall():
        access = cipher.encrypt(
            row["access_token"].encode(),
            associated_data=token_associated_data(
                row["tenant_key"], row["app_id"], row["subject_id"], "access"
            ),
        )
        refresh = cipher.encrypt(
            row["refresh_token"].encode(),
            associated_data=token_associated_data(
                row["tenant_key"], row["app_id"], row["subject_id"], "refresh"
            ),
        )
        await db.execute(
            """
            UPDATE oauth_tokens
            SET access_token = '', refresh_token = '',
                access_token_ciphertext = ?, access_token_nonce = ?,
                refresh_token_ciphertext = ?, refresh_token_nonce = ?,
                encryption_key_version = ?
            WHERE tenant_key = ? AND app_id = ? AND subject_id = ?
            """,
            (
                access.ciphertext,
                access.nonce,
                refresh.ciphertext,
                refresh.nonce,
                access.key_version,
                row["tenant_key"],
                row["app_id"],
                row["subject_id"],
            ),
        )


async def _reliable_pending_actions(
    db: aiosqlite.Connection,
    cipher: AesGcmTokenCipher,
) -> None:
    del cipher
    columns = await _columns(db, "pending_actions")
    if "claimed_by" not in columns:
        await db.execute("DROP TABLE IF EXISTS action_attempts")
        await db.execute("DROP TABLE IF EXISTS action_outbox")
        await db.execute("ALTER TABLE pending_actions RENAME TO pending_actions_legacy")
        await db.execute(
            """
            CREATE TABLE pending_actions (
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
                last_error_message TEXT NOT NULL DEFAULT ''
            )
            """
        )
        await db.execute(
            """
            INSERT INTO pending_actions(
                action_id, tenant_key, app_id, chat_id, thread_id,
                creator_subject_id, creator_open_id, action_type, payload_json,
                payload_hash, source_message_id, status, expires_at, created_at,
                consumed_at
            )
            SELECT action_id, tenant_key, app_id, chat_id, thread_id,
                   creator_subject_id, creator_open_id, action_type, payload_json,
                   payload_hash, source_message_id,
                   CASE status
                       WHEN 'pending' THEN 'awaiting_confirmation'
                       WHEN 'completed' THEN 'succeeded'
                       ELSE status
                   END,
                   expires_at, created_at, consumed_at
            FROM pending_actions_legacy
            """
        )
        await db.execute("DROP TABLE pending_actions_legacy")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_pending_actions_expiry "
            "ON pending_actions(status, expires_at)"
        )
        # pending_actions 已原地迁移，重建关联执行表。
        await db.execute("DROP TABLE IF EXISTS action_attempts")
        await db.execute("DROP TABLE IF EXISTS action_outbox")
        await db.execute(
            """
            CREATE TABLE action_attempts (
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
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE action_outbox (
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
            )
            """
        )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_action_outbox_ready "
        "ON action_outbox(status, available_at)"
    )


async def _scheduler_lease(
    db: aiosqlite.Connection,
    cipher: AesGcmTokenCipher,
) -> None:
    del cipher
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS scheduler_leases (
            lease_name TEXT PRIMARY KEY,
            holder_id TEXT NOT NULL,
            acquired_at INTEGER NOT NULL,
            renewed_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            fencing_token INTEGER NOT NULL
        )
        """
    )


async def _audit_log_v2(
    db: aiosqlite.Connection,
    cipher: AesGcmTokenCipher,
) -> None:
    del cipher
    columns = await _columns(db, "audit_logs")
    if "audit_id" in columns:
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_logs_actor "
            "ON audit_logs(tenant_key, app_id, principal_id, occurred_at)"
        )
        return
    await db.execute("ALTER TABLE audit_logs RENAME TO audit_logs_legacy")
    await db.execute(
        """
        CREATE TABLE audit_logs (
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
        )
        """
    )
    await db.execute(
        """
        INSERT INTO audit_logs(
            audit_id, occurred_at, trace_id, tenant_key, app_id, principal_id,
            action_id, operation, result, metadata_json
        )
        SELECT lower(hex(randomblob(16))), occurred_at, 'legacy',
               tenant_key, app_id, subject_id, action_id, action_type,
               outcome, details_json
        FROM audit_logs_legacy
        """
    )
    await db.execute("DROP TABLE audit_logs_legacy")
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_logs_actor "
        "ON audit_logs(tenant_key, app_id, principal_id, occurred_at)"
    )


async def _oauth_refresh_lease(
    db: aiosqlite.Connection,
    cipher: AesGcmTokenCipher,
) -> None:
    del cipher
    columns = await _columns(db, "oauth_tokens")
    additions = {
        "refresh_status": "TEXT NOT NULL DEFAULT 'idle'",
        "refresh_lease_owner": "TEXT",
        "refresh_lease_expires_at": "INTEGER",
        "refresh_attempts": "INTEGER NOT NULL DEFAULT 0",
        "token_version": "INTEGER NOT NULL DEFAULT 1",
        "last_refresh_success_at": "INTEGER",
        "last_refresh_failure_at": "INTEGER",
    }
    for name, definition in additions.items():
        if name not in columns:
            await db.execute(f"ALTER TABLE oauth_tokens ADD COLUMN {name} {definition}")


async def _scheduled_jobs_v2(
    db: aiosqlite.Connection,
    cipher: AesGcmTokenCipher,
) -> None:
    del cipher
    columns = await _columns(db, "scheduled_jobs")
    if "tenant_key" in columns:
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_due "
            "ON scheduled_jobs(status, run_at, next_retry_at)"
        )
        return
    await db.execute("ALTER TABLE scheduled_jobs RENAME TO scheduled_jobs_legacy")
    await db.execute(
        """
        CREATE TABLE scheduled_jobs (
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
        )
        """
    )
    await db.execute("DROP TABLE scheduled_jobs_legacy")
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_due "
        "ON scheduled_jobs(status, run_at, next_retry_at)"
    )


async def _agent_history(
    db: aiosqlite.Connection,
    cipher: AesGcmTokenCipher,
) -> None:
    del cipher
    await db.execute(
        """
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
        )
        """
    )
    await db.execute(
        """
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
        )
        """
    )


async def _oauth_original_request(
    db: aiosqlite.Connection,
    cipher: AesGcmTokenCipher,
) -> None:
    del cipher
    columns = await _columns(db, "oauth_authorizations")
    if "original_request" not in columns:
        await db.execute(
            "ALTER TABLE oauth_authorizations ADD COLUMN original_request TEXT NOT NULL DEFAULT ''"
        )


async def _business_pending_actions(
    db: aiosqlite.Connection,
    cipher: AesGcmTokenCipher,
) -> None:
    """增加业务动作字段并原地迁移旧数据。"""

    del cipher
    columns = await _columns(db, "pending_actions")
    additions = {
        "payload_version": "INTEGER NOT NULL DEFAULT 1",
        "idempotency_key": "TEXT NOT NULL DEFAULT ''",
        "source": "TEXT NOT NULL DEFAULT 'legacy_agent'",
        "source_card_message_id": "TEXT NOT NULL DEFAULT ''",
        "result_json": "TEXT NOT NULL DEFAULT '{}'",
        "updated_at": "INTEGER NOT NULL DEFAULT 0",
    }
    for name, definition in additions.items():
        if name not in columns:
            await db.execute(f"ALTER TABLE pending_actions ADD COLUMN {name} {definition}")
    await db.execute(
        """
        UPDATE pending_actions
        SET payload_version = 1,
            source = CASE WHEN source = '' THEN 'legacy_agent' ELSE source END,
            updated_at = CASE WHEN updated_at = 0 THEN created_at ELSE updated_at END
        """
    )
    await db.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_actions_idempotency
        ON pending_actions(tenant_key, app_id, idempotency_key)
        WHERE idempotency_key != ''
        """
    )


async def _context_memory(
    db: aiosqlite.Connection,
    cipher: AesGcmTokenCipher,
) -> None:
    """增加分层会话、摘要、长期记忆和工具结果存储。"""

    del cipher
    statements = """
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
        CREATE INDEX IF NOT EXISTS idx_conversation_messages_recent
        ON conversation_messages(
            tenant_key, app_id, principal_id, session_id, created_at
        );
        CREATE INDEX IF NOT EXISTS idx_user_memories_principal
        ON user_memories(tenant_key, app_id, principal_id, updated_at);
        """
    for statement in statements.split(";"):
        if statement.strip():
            await db.execute(statement)


async def _columns(db: aiosqlite.Connection, table: str) -> set[str]:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    return {row["name"] for row in await cursor.fetchall()}


def _encrypted(row: aiosqlite.Row, kind: str):
    from ..services.encryption import EncryptedValue

    return EncryptedValue(
        ciphertext=row[f"{kind}_token_ciphertext"] or b"",
        nonce=row[f"{kind}_token_nonce"] or b"",
        key_version=row["encryption_key_version"],
    )
