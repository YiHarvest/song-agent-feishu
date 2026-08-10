from __future__ import annotations

import io

import pytest

from song_agent.adapters.attachments import LocalAttachmentStore
from song_agent.infrastructure.database import Database
from song_agent.infrastructure.repositories import SqliteAttachmentRepository
from song_agent.kernel.models import PrincipalIdentity


@pytest.mark.asyncio
async def test_attachment_ingress_only_stores_and_scopes_file(database_path, tmp_path) -> None:
    repository = SqliteAttachmentRepository(Database(database_path))
    store = LocalAttachmentStore(
        root=tmp_path / "attachments",
        repository=repository,
        max_bytes=1024,
        ttl_seconds=3600,
    )
    owner = PrincipalIdentity(tenant_id="t", principal_id="owner", auth_provider="test")
    attachment = await store.save(
        principal=owner,
        conversation=None,
        filename="../safe.txt",
        media_type="text/plain",
        stream=io.BytesIO(b"content"),
    )
    assert attachment.filename == "safe.txt"
    assert attachment.sha256
    path = await store.resolve_path(attachment, principal=owner, conversation=None)
    assert path.read_bytes() == b"content"
    stranger = PrincipalIdentity(tenant_id="t", principal_id="other", auth_provider="test")
    with pytest.raises(PermissionError):
        await store.resolve_path(attachment, principal=stranger, conversation=None)
