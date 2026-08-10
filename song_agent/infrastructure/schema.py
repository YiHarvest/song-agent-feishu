"""Single SQLAlchemy metadata used only by Alembic and schema inspection."""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)

metadata = MetaData()

executions = Table(
    "executions",
    metadata,
    Column("execution_id", String(96), primary_key=True),
    Column("tenant_id", String(255), nullable=False),
    Column("principal_id", String(255), nullable=False),
    Column("intent_id", String(255), nullable=False),
    Column("intent_version", Integer, nullable=False),
    Column("idempotency_key", String(255), nullable=False),
    Column("payload_hash", String(64), nullable=False),
    Column("status", String(32), nullable=False),
    Column("result_json", Text, nullable=True),
    Column("provider_request_id", String(255), nullable=False, server_default=""),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
    UniqueConstraint("tenant_id", "principal_id", "intent_id", "idempotency_key"),
)

actions = Table(
    "actions",
    metadata,
    Column("action_id", String(96), primary_key=True),
    Column("intent_id", String(255), nullable=False),
    Column("intent_version", Integer, nullable=False),
    Column("module_id", String(96), nullable=False),
    Column("module_version", String(64), nullable=False),
    Column("arguments_json", Text, nullable=False),
    Column("principal_json", Text, nullable=False),
    Column("delivery_target_json", Text, nullable=False),
    Column("requested_by_json", Text, nullable=False),
    Column("requested_tenant_id", String(255), nullable=False),
    Column("requested_principal_id", String(255), nullable=False),
    Column("confirmed_by_json", Text, nullable=True),
    Column("risk_level", String(32), nullable=False),
    Column("permissions_json", Text, nullable=False),
    Column("payload_hash", String(64), nullable=False),
    Column("idempotency_key", String(255), nullable=False),
    Column("status", String(32), nullable=False),
    Column("result_json", Text, nullable=True),
    Column("expires_at", String(40), nullable=False),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
    UniqueConstraint(
        "requested_tenant_id",
        "requested_principal_id",
        "intent_id",
        "idempotency_key",
    ),
)

audit_events = Table(
    "audit_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("trace_id", String(96), nullable=False),
    Column("request_id", String(255), nullable=False),
    Column("tenant_id", String(255), nullable=False),
    Column("principal_id", String(255), nullable=False),
    Column("channel", String(80), nullable=False),
    Column("intent_id", String(255), nullable=False),
    Column("intent_version", Integer, nullable=False),
    Column("module_id", String(96), nullable=False),
    Column("phase", String(64), nullable=False),
    Column("outcome", String(64), nullable=False),
    Column("payload_hash", String(64), nullable=False),
    Column("metadata_json", Text, nullable=False),
    Column("created_at", String(40), nullable=False),
)

conversation_messages = Table(
    "conversation_messages",
    metadata,
    Column("message_id", String(255), primary_key=True),
    Column("conversation_key", String(1024), nullable=False, index=True),
    Column("role", String(32), nullable=False),
    Column("content", Text, nullable=False),
    Column("created_at", String(40), nullable=False),
)

conversation_summaries = Table(
    "conversation_summaries",
    metadata,
    Column("conversation_key", String(1024), primary_key=True),
    Column("summary", Text, nullable=False),
    Column("updated_at", String(40), nullable=False),
)

memories = Table(
    "memories",
    metadata,
    Column("tenant_id", String(255), primary_key=True),
    Column("principal_id", String(255), primary_key=True),
    Column("memory_key", String(255), primary_key=True),
    Column("value", Text, nullable=False),
    Column("updated_at", String(40), nullable=False),
)

attachments = Table(
    "attachments",
    metadata,
    Column("attachment_id", String(255), primary_key=True),
    Column("tenant_id", String(255), nullable=False),
    Column("principal_id", String(255), nullable=False),
    Column("conversation_key", String(1024), nullable=False, server_default=""),
    Column("media_type", String(255), nullable=False),
    Column("filename", String(1024), nullable=False),
    Column("size_bytes", Integer, nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("expires_at", String(40), nullable=True),
    Column("storage_key", String(2048), nullable=False),
    Column("temporary", Boolean, nullable=False),
    Column("created_at", String(40), nullable=False),
)

delivery_bindings = Table(
    "delivery_bindings",
    metadata,
    Column("binding_id", String(255), primary_key=True),
    Column("tenant_id", String(255), nullable=False),
    Column("principal_id", String(255), nullable=False),
    Column("target_json", Text, nullable=False),
    Column("updated_at", String(40), nullable=False),
)

api_keys = Table(
    "api_keys",
    metadata,
    Column("key_id", String(96), primary_key=True),
    Column("key_hash", String(64), nullable=False, unique=True),
    Column("tenant_id", String(255), nullable=False),
    Column("principal_id", String(255), nullable=False),
    Column("enabled", Boolean, nullable=False, server_default="1"),
    Column("created_at", String(40), nullable=False),
)

oauth_tokens = Table(
    "oauth_tokens",
    metadata,
    Column("tenant_id", String(255), primary_key=True),
    Column("principal_id", String(255), primary_key=True),
    Column("provider_id", String(96), primary_key=True),
    Column("ciphertext", LargeBinary, nullable=False),
    Column("nonce", LargeBinary, nullable=False),
    Column("scopes_json", Text, nullable=False),
    Column("expires_at", String(40), nullable=True),
    Column("updated_at", String(40), nullable=False),
)

oauth_states = Table(
    "oauth_states",
    metadata,
    Column("state_hash", String(64), primary_key=True),
    Column("principal_json", Text, nullable=False),
    Column("delivery_target_json", Text, nullable=False),
    Column("required_scopes_json", Text, nullable=False),
    Column("expires_at", String(40), nullable=False),
    Column("created_at", String(40), nullable=False),
)

processed_events = Table(
    "processed_events",
    metadata,
    Column("event_id", String(512), primary_key=True),
    Column("source", String(80), nullable=False),
    Column("processed_at", String(40), nullable=False),
)

scheduler_leases = Table(
    "scheduler_leases",
    metadata,
    Column("job_id", String(255), primary_key=True),
    Column("owner_id", String(255), nullable=False),
    Column("lease_until", String(40), nullable=False),
    Column("fencing_token", Integer, nullable=False),
    Column("updated_at", String(40), nullable=False),
)

module_records = Table(
    "module_records",
    metadata,
    Column("module_id", String(96), primary_key=True),
    Column("record_type", String(96), primary_key=True),
    Column("record_id", String(255), primary_key=True),
    Column("tenant_id", String(255), nullable=False),
    Column("principal_id", String(255), nullable=False),
    Column("payload_json", Text, nullable=False),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
)
