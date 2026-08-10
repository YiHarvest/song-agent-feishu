"""Fresh v1 schema with no legacy import path.

This migration intentionally carries a frozen DDL snapshot. It must not import the
live application metadata, because future model changes may not rewrite history.
"""

from alembic import op

revision = "0001_v1"
down_revision = None
branch_labels = None
depends_on = None

_DDL = (
    """CREATE TABLE executions (
        execution_id VARCHAR(96) NOT NULL PRIMARY KEY,
        tenant_id VARCHAR(255) NOT NULL,
        principal_id VARCHAR(255) NOT NULL,
        intent_id VARCHAR(255) NOT NULL,
        intent_version INTEGER NOT NULL,
        idempotency_key VARCHAR(255) NOT NULL,
        payload_hash VARCHAR(64) NOT NULL,
        status VARCHAR(32) NOT NULL,
        result_json TEXT,
        provider_request_id VARCHAR(255) DEFAULT '' NOT NULL,
        created_at VARCHAR(40) NOT NULL,
        updated_at VARCHAR(40) NOT NULL,
        UNIQUE (tenant_id, principal_id, intent_id, idempotency_key)
    )""",
    """CREATE TABLE actions (
        action_id VARCHAR(96) NOT NULL PRIMARY KEY,
        intent_id VARCHAR(255) NOT NULL,
        intent_version INTEGER NOT NULL,
        module_id VARCHAR(96) NOT NULL,
        module_version VARCHAR(64) NOT NULL,
        arguments_json TEXT NOT NULL,
        principal_json TEXT NOT NULL,
        delivery_target_json TEXT NOT NULL,
        requested_by_json TEXT NOT NULL,
        requested_tenant_id VARCHAR(255) NOT NULL,
        requested_principal_id VARCHAR(255) NOT NULL,
        confirmed_by_json TEXT,
        risk_level VARCHAR(32) NOT NULL,
        permissions_json TEXT NOT NULL,
        payload_hash VARCHAR(64) NOT NULL,
        idempotency_key VARCHAR(255) NOT NULL,
        status VARCHAR(32) NOT NULL,
        result_json TEXT,
        expires_at VARCHAR(40) NOT NULL,
        created_at VARCHAR(40) NOT NULL,
        updated_at VARCHAR(40) NOT NULL,
        UNIQUE (requested_tenant_id, requested_principal_id, intent_id, idempotency_key)
    )""",
    """CREATE TABLE audit_events (
        id INTEGER NOT NULL PRIMARY KEY,
        trace_id VARCHAR(96) NOT NULL,
        request_id VARCHAR(255) NOT NULL,
        tenant_id VARCHAR(255) NOT NULL,
        principal_id VARCHAR(255) NOT NULL,
        channel VARCHAR(80) NOT NULL,
        intent_id VARCHAR(255) NOT NULL,
        intent_version INTEGER NOT NULL,
        module_id VARCHAR(96) NOT NULL,
        phase VARCHAR(64) NOT NULL,
        outcome VARCHAR(64) NOT NULL,
        payload_hash VARCHAR(64) NOT NULL,
        metadata_json TEXT NOT NULL,
        created_at VARCHAR(40) NOT NULL
    )""",
    """CREATE TABLE conversation_messages (
        message_id VARCHAR(255) NOT NULL PRIMARY KEY,
        conversation_key VARCHAR(1024) NOT NULL,
        role VARCHAR(32) NOT NULL,
        content TEXT NOT NULL,
        created_at VARCHAR(40) NOT NULL
    )""",
    """CREATE INDEX ix_conversation_messages_conversation_key
        ON conversation_messages (conversation_key)""",
    """CREATE TABLE conversation_summaries (
        conversation_key VARCHAR(1024) NOT NULL PRIMARY KEY,
        summary TEXT NOT NULL,
        updated_at VARCHAR(40) NOT NULL
    )""",
    """CREATE TABLE memories (
        tenant_id VARCHAR(255) NOT NULL,
        principal_id VARCHAR(255) NOT NULL,
        memory_key VARCHAR(255) NOT NULL,
        value TEXT NOT NULL,
        updated_at VARCHAR(40) NOT NULL,
        PRIMARY KEY (tenant_id, principal_id, memory_key)
    )""",
    """CREATE TABLE attachments (
        attachment_id VARCHAR(255) NOT NULL PRIMARY KEY,
        tenant_id VARCHAR(255) NOT NULL,
        principal_id VARCHAR(255) NOT NULL,
        conversation_key VARCHAR(1024) DEFAULT '' NOT NULL,
        media_type VARCHAR(255) NOT NULL,
        filename VARCHAR(1024) NOT NULL,
        size_bytes INTEGER NOT NULL,
        sha256 VARCHAR(64) NOT NULL,
        expires_at VARCHAR(40),
        storage_key VARCHAR(2048) NOT NULL,
        temporary BOOLEAN NOT NULL,
        created_at VARCHAR(40) NOT NULL
    )""",
    """CREATE TABLE delivery_bindings (
        binding_id VARCHAR(255) NOT NULL PRIMARY KEY,
        tenant_id VARCHAR(255) NOT NULL,
        principal_id VARCHAR(255) NOT NULL,
        target_json TEXT NOT NULL,
        updated_at VARCHAR(40) NOT NULL
    )""",
    """CREATE TABLE api_keys (
        key_id VARCHAR(96) NOT NULL PRIMARY KEY,
        key_hash VARCHAR(64) NOT NULL,
        tenant_id VARCHAR(255) NOT NULL,
        principal_id VARCHAR(255) NOT NULL,
        enabled BOOLEAN DEFAULT '1' NOT NULL,
        created_at VARCHAR(40) NOT NULL,
        UNIQUE (key_hash)
    )""",
    """CREATE TABLE oauth_tokens (
        tenant_id VARCHAR(255) NOT NULL,
        principal_id VARCHAR(255) NOT NULL,
        provider_id VARCHAR(96) NOT NULL,
        ciphertext BLOB NOT NULL,
        nonce BLOB NOT NULL,
        scopes_json TEXT NOT NULL,
        expires_at VARCHAR(40),
        updated_at VARCHAR(40) NOT NULL,
        PRIMARY KEY (tenant_id, principal_id, provider_id)
    )""",
    """CREATE TABLE processed_events (
        event_id VARCHAR(512) NOT NULL PRIMARY KEY,
        source VARCHAR(80) NOT NULL,
        processed_at VARCHAR(40) NOT NULL
    )""",
    """CREATE TABLE scheduler_leases (
        job_id VARCHAR(255) NOT NULL PRIMARY KEY,
        owner_id VARCHAR(255) NOT NULL,
        lease_until VARCHAR(40) NOT NULL,
        fencing_token INTEGER NOT NULL,
        updated_at VARCHAR(40) NOT NULL
    )""",
    """CREATE TABLE module_records (
        module_id VARCHAR(96) NOT NULL,
        record_type VARCHAR(96) NOT NULL,
        record_id VARCHAR(255) NOT NULL,
        tenant_id VARCHAR(255) NOT NULL,
        principal_id VARCHAR(255) NOT NULL,
        payload_json TEXT NOT NULL,
        created_at VARCHAR(40) NOT NULL,
        updated_at VARCHAR(40) NOT NULL,
        PRIMARY KEY (module_id, record_type, record_id)
    )""",
)


def upgrade() -> None:
    for statement in _DDL:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("song-agent schema migrations are forward-only")
