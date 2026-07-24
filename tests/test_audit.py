import json
from pathlib import Path

import pytest

from song_agent.observability.context import trace_scope
from song_agent.services.audit import AuditService
from song_agent.services.encryption import AesGcmTokenCipher
from song_agent.store import SqliteStore


@pytest.mark.asyncio
async def test_audit_has_trace_and_redacts_secrets_and_document_content(
    tmp_path: Path,
) -> None:
    store = SqliteStore(
        tmp_path / "state.db",
        app_id="app",
        token_cipher=AesGcmTokenCipher({1: b"1" * 32}, 1),
    )
    await store.initialize()
    try:
        audit = AuditService(store)
        with trace_scope("trace-123"):
            await audit.record(
                "document.create",
                "success",
                principal_id="principal",
                action_id="action",
                metadata={
                    "access_token": "secret-token",
                    "markdown": "full document body",
                    "created_count": 1,
                },
            )
        row = await (await store.db.execute("SELECT * FROM audit_logs")).fetchone()
        metadata = json.loads(row["metadata_json"])
        assert row["trace_id"] == "trace-123"
        assert row["action_id"] == "action"
        assert metadata["access_token"] == "***"
        assert metadata["markdown"] == "***"
        assert metadata["created_count"] == 1
        assert "secret-token" not in row["metadata_json"]
        assert "full document body" not in row["metadata_json"]
    finally:
        await store.close()
