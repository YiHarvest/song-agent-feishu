from __future__ import annotations

import hashlib
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO

from ..kernel.models import AttachmentRef, ConversationRef, PrincipalIdentity
from ..ports.repositories import AttachmentRepository


class LocalAttachmentStore:
    def __init__(
        self,
        *,
        root: Path,
        repository: AttachmentRepository,
        max_bytes: int,
        ttl_seconds: int,
    ) -> None:
        self.root = root.resolve()
        self.repository = repository
        self.max_bytes = max_bytes
        self.ttl_seconds = ttl_seconds

    async def save(
        self,
        *,
        principal: PrincipalIdentity,
        conversation: ConversationRef | None,
        filename: str,
        media_type: str,
        stream: BinaryIO,
        temporary: bool = True,
    ) -> AttachmentRef:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        attachment_id = f"att_{uuid.uuid4().hex}"
        temporary_path = self.root / f".{attachment_id}.partial"
        destination = self.root / attachment_id
        digest = hashlib.sha256()
        size = 0
        try:
            with temporary_path.open("xb") as output:
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise ValueError("attachment exceeds configured size limit")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            temporary_path.replace(destination)
            destination.chmod(0o600)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise
        expires_at = (
            datetime.now(UTC) + timedelta(seconds=self.ttl_seconds) if temporary else None
        )
        attachment = AttachmentRef(
            attachment_id=attachment_id,
            media_type=media_type,
            filename=Path(filename).name,
            size_bytes=size,
            sha256=digest.hexdigest(),
            expires_at=expires_at,
        )
        await self.repository.create(
            attachment=attachment,
            principal=principal,
            conversation=conversation,
            storage_key=attachment_id,
            temporary=temporary,
        )
        return attachment

    async def resolve_path(
        self,
        attachment: AttachmentRef,
        *,
        principal: PrincipalIdentity,
        conversation: ConversationRef | None,
    ) -> Path:
        owned = await self.repository.get_owned(
            attachment.attachment_id,
            principal=principal,
            conversation=conversation,
        )
        if owned is None or owned[0].sha256 != attachment.sha256:
            raise PermissionError("attachment is missing, expired, or outside the current scope")
        path = (self.root / owned[1]).resolve()
        if path.parent != self.root or not path.is_file():
            raise PermissionError("invalid attachment storage reference")
        return path
