from __future__ import annotations

import time
import uuid
from pathlib import Path

from ..store import SqliteStore
from .models import Attachment, AttachmentAccess, AttachmentKind, AttachmentStatus


class AttachmentRepository:
    def __init__(self, store: SqliteStore) -> None:
        self.store = store

    async def create(
        self,
        *,
        access: AttachmentAccess,
        source_message_id: str,
        source_resource_key: str,
        kind: AttachmentKind,
        filename: str,
        storage_path: Path,
        ttl_seconds: int,
    ) -> Attachment:
        now = int(time.time())
        attachment = Attachment(
            attachment_id=f"att_{uuid.uuid4().hex}",
            tenant_key=access.tenant_key,
            app_id=access.app_id,
            principal_id=access.principal_id,
            source="feishu",
            source_message_id=source_message_id,
            source_resource_key=source_resource_key,
            attachment_kind=kind,
            filename=filename,
            media_type="",
            size_bytes=0,
            sha256="",
            storage_path=storage_path,
            status="downloading",
            created_at=now,
            updated_at=now,
            expires_at=now + ttl_seconds,
        )
        await self.store.db.execute(
            """
            INSERT INTO attachments(
                attachment_id, tenant_key, app_id, principal_id, source,
                source_message_id, source_resource_key, attachment_kind, filename,
                media_type, size_bytes, sha256, storage_path, status,
                created_at, updated_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attachment.attachment_id,
                attachment.tenant_key,
                attachment.app_id,
                attachment.principal_id,
                attachment.source,
                attachment.source_message_id,
                attachment.source_resource_key,
                attachment.attachment_kind,
                attachment.filename,
                attachment.media_type,
                attachment.size_bytes,
                attachment.sha256,
                str(attachment.storage_path),
                attachment.status,
                attachment.created_at,
                attachment.updated_at,
                attachment.expires_at,
            ),
        )
        await self.store.db.commit()
        return attachment

    async def get_owned(
        self,
        attachment_id: str,
        access: AttachmentAccess,
    ) -> Attachment | None:
        row = await (
            await self.store.db.execute(
                """
                SELECT * FROM attachments
                WHERE attachment_id = ? AND tenant_key = ? AND app_id = ?
                  AND principal_id = ? AND status != 'expired'
                  AND (expires_at IS NULL OR expires_at > ?)
                """,
                (
                    attachment_id,
                    access.tenant_key,
                    access.app_id,
                    access.principal_id,
                    int(time.time()),
                ),
            )
        ).fetchone()
        return Attachment.model_validate(dict(row)) if row else None

    async def update_ready(
        self,
        attachment_id: str,
        *,
        storage_path: Path,
        filename: str,
        media_type: str,
        size_bytes: int,
        sha256: str,
    ) -> None:
        await self.store.db.execute(
            """
            UPDATE attachments
            SET storage_path = ?, filename = ?, media_type = ?, size_bytes = ?, sha256 = ?,
                status = 'ready', updated_at = ?
            WHERE attachment_id = ? AND status = 'downloading'
            """,
            (
                str(storage_path),
                filename,
                media_type,
                size_bytes,
                sha256,
                int(time.time()),
                attachment_id,
            ),
        )
        await self.store.db.commit()

    async def transition(
        self,
        attachment_id: str,
        *,
        from_statuses: tuple[AttachmentStatus, ...],
        to_status: AttachmentStatus,
    ) -> bool:
        placeholders = ",".join("?" for _ in from_statuses)
        cursor = await self.store.db.execute(
            f"""
            UPDATE attachments SET status = ?, updated_at = ?
            WHERE attachment_id = ? AND status IN ({placeholders})
            """,
            (to_status, int(time.time()), attachment_id, *from_statuses),
        )
        await self.store.db.commit()
        return cursor.rowcount == 1

    async def recover_interrupted(self) -> int:
        """Mark transient rows left by a stopped/reloaded worker as failed."""

        cursor = await self.store.db.execute(
            """
            UPDATE attachments SET status = 'failed', updated_at = ?
            WHERE status IN ('downloading', 'parsing')
            """,
            (int(time.time()),),
        )
        await self.store.db.commit()
        return cursor.rowcount

    async def expired_candidates(self, now: int) -> list[Attachment]:
        rows = await (
            await self.store.db.execute(
                """
                SELECT * FROM attachments
                WHERE expires_at IS NOT NULL AND expires_at <= ?
                  AND status NOT IN ('parsing', 'expired')
                """,
                (now,),
            )
        ).fetchall()
        return [Attachment.model_validate(dict(row)) for row in rows]
