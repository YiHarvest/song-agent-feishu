from __future__ import annotations

from pathlib import Path
from typing import Any, BinaryIO, Protocol

from ..kernel.models import AttachmentRef, ConversationRef, PrincipalIdentity


class AttachmentStorePort(Protocol):
    async def save(
        self,
        *,
        principal: PrincipalIdentity,
        conversation: ConversationRef | None,
        filename: str,
        media_type: str,
        stream: BinaryIO,
        temporary: bool = True,
    ) -> AttachmentRef: ...

    async def resolve_path(
        self,
        attachment: AttachmentRef,
        *,
        principal: PrincipalIdentity,
        conversation: ConversationRef | None,
    ) -> Path: ...


class VisionPort(Protocol):
    async def analyze(self, path: Path, media_type: str, instruction: str) -> dict[str, Any]: ...


class AsrPort(Protocol):
    async def transcribe(
        self,
        path: Path,
        *,
        filename: str,
        media_type: str,
        language: str,
    ) -> dict[str, Any]: ...


class DocumentParserPort(Protocol):
    async def parse(
        self,
        path: Path,
        *,
        filename: str,
        media_type: str,
        instruction: str,
    ) -> dict[str, Any]: ...
