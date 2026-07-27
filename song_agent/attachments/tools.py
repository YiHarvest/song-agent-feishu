from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from ..agent.models import ToolResult
from ..config import Settings
from ..media.asr_client import AsrClient
from ..media.vision_client import VisionClient
from ..parsers.document_client import MinerUDocumentClient
from ..parsers.local_text_parser import parse_local_text
from ..store import SqliteStore
from .models import (
    AnalyzeImageInput,
    AnalyzeImageResult,
    Attachment,
    AttachmentAccess,
    ParseDocumentInput,
    ParseDocumentResult,
    TranscribeAudioInput,
    TranscribeAudioResult,
)
from .repository import AttachmentRepository
from .storage import ensure_within_root


class AttachmentTools:
    def __init__(
        self,
        settings: Settings,
        store: SqliteStore,
        repository: AttachmentRepository,
        vision: VisionClient,
        asr: AsrClient,
        documents: MinerUDocumentClient,
    ) -> None:
        self.settings = settings
        self.store = store
        self.repository = repository
        self.vision = vision
        self.asr = asr
        self.documents = documents
        self.root = settings.song_agent_attachment_dir.expanduser().resolve()

    async def analyze_image(
        self,
        access: AttachmentAccess,
        value: AnalyzeImageInput,
    ) -> AnalyzeImageResult:
        attachment = await self._claim(value.attachment_id, access, "image")
        try:
            payload = await self.vision.analyze(
                ensure_within_root(attachment.storage_path, self.root),
                attachment.media_type,
                value.instruction,
            )
            result_ref = await self._save(
                access,
                "attachments.analyze_image",
                payload,
                "图片已解析",
            )
            await self._finish(attachment.attachment_id, True)
            return AnalyzeImageResult(
                attachment_id=attachment.attachment_id,
                result_ref=result_ref,
                **payload,
            )
        except asyncio.CancelledError:
            await self._finish(attachment.attachment_id, False)
            raise
        except Exception:
            await self._finish(attachment.attachment_id, False)
            raise

    async def transcribe_audio(
        self,
        access: AttachmentAccess,
        value: TranscribeAudioInput,
    ) -> TranscribeAudioResult:
        attachment = await self._claim(value.attachment_id, access, "audio")
        try:
            payload = await self.asr.transcribe(
                ensure_within_root(attachment.storage_path, self.root),
                filename=attachment.filename,
                media_type=attachment.media_type,
                language=value.language,
            )
            result_ref = await self._save(
                access,
                "attachments.transcribe_audio",
                payload,
                "语音已转写",
            )
            await self._finish(attachment.attachment_id, True)
            return TranscribeAudioResult(
                attachment_id=attachment.attachment_id,
                result_ref=result_ref,
                **payload,
            )
        except asyncio.CancelledError:
            await self._finish(attachment.attachment_id, False)
            raise
        except Exception:
            await self._finish(attachment.attachment_id, False)
            raise

    async def parse_document(
        self,
        access: AttachmentAccess,
        value: ParseDocumentInput,
    ) -> ParseDocumentResult:
        attachment = await self._claim(value.attachment_id, access, "document")
        try:
            path = ensure_within_root(attachment.storage_path, self.root)
            local_types = {"text/plain", "text/markdown", "text/csv", "application/json"}
            if attachment.media_type in local_types:
                content, metadata = parse_local_text(
                    path,
                    attachment.media_type,
                    max_chars=self.settings.song_agent_document_max_file_mb * 1024 * 1024,
                )
                metadata["provider"] = "local"
            else:
                content, metadata = await self.documents.parse(
                    attachment_id=attachment.attachment_id,
                    local_path=path,
                )
            preview_limit = self.settings.song_agent_document_max_preview_chars
            context_limit = self.settings.song_agent_document_max_context_chars
            title, outline = _document_structure(content, attachment.filename)
            summary = _summary(content, value.instruction, context_limit)
            truncated = len(content) > preview_limit
            full_payload = {
                "attachment_id": attachment.attachment_id,
                "filename": attachment.filename,
                "media_type": attachment.media_type,
                "title": title,
                "content": content,
                "metadata": metadata,
            }
            result_ref = await self._save(
                access,
                "attachments.parse_document",
                full_payload,
                summary,
                truncated=truncated,
            )
            await self._finish(attachment.attachment_id, True)
            return ParseDocumentResult(
                attachment_id=attachment.attachment_id,
                filename=attachment.filename,
                media_type=attachment.media_type,
                title=title,
                summary=summary,
                outline=outline,
                content_preview=content[:preview_limit],
                result_ref=result_ref,
                truncated=truncated,
                metadata=metadata,
            )
        except asyncio.CancelledError:
            await self._finish(attachment.attachment_id, False)
            raise
        except Exception:
            await self._finish(attachment.attachment_id, False)
            raise

    async def as_tool_result(self, result: Any, *, summary: str) -> ToolResult:
        payload = result.model_dump(mode="json")
        response = str(payload)
        return ToolResult(status="ok", summary=summary, response=response[:12_000])

    async def _claim(
        self,
        attachment_id: str,
        access: AttachmentAccess,
        expected_kind: str,
    ) -> Attachment:
        attachment = await self.repository.get_owned(attachment_id, access)
        if attachment is None:
            raise ValueError("附件不存在、已过期或无权访问")
        if attachment.attachment_kind != expected_kind:
            raise ValueError("附件类型与工具不匹配")
        claimed = await self.repository.transition(
            attachment_id,
            from_statuses=("ready", "parsed", "failed"),
            to_status="parsing",
        )
        if not claimed:
            raise RuntimeError("附件当前不可解析")
        return attachment

    async def _finish(self, attachment_id: str, success: bool) -> None:
        await self.repository.transition(
            attachment_id,
            from_statuses=("parsing",),
            to_status="parsed" if success else "failed",
        )

    async def _save(
        self,
        access: AttachmentAccess,
        tool_name: str,
        payload: dict[str, Any],
        summary: str,
        *,
        truncated: bool = False,
    ) -> str:
        return await self.store.save_tool_result(
            tenant_key=access.tenant_key,
            app_id=access.app_id,
            principal_id=access.principal_id,
            tool_name=tool_name,
            summary=summary[:2000],
            payload=payload,
            truncated=truncated,
            expires_at=int(time.time()) + self.settings.song_agent_attachment_ttl_seconds,
        )


def _document_structure(content: str, filename: str) -> tuple[str | None, list[str]]:
    headings = [
        re.sub(r"^#{1,6}\s+", "", line).strip()
        for line in content.splitlines()
        if re.match(r"^#{1,6}\s+\S", line)
    ]
    title = headings[0] if headings else filename.rsplit(".", 1)[0] or None
    return title, headings[:20]


def _summary(content: str, instruction: str, max_chars: int) -> str:
    compact = re.sub(r"\s+", " ", content).strip()
    prefix = f"解析要求：{instruction.strip()}；" if instruction.strip() else ""
    return (prefix + compact)[:max_chars]
