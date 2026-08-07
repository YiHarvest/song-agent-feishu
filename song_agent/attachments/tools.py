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
from .parse_cache import (
    AttachmentParseResultRepository,
    build_key,
    normalize_instruction,
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
        parse_cache: AttachmentParseResultRepository | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.repository = repository
        self.vision = vision
        self.asr = asr
        self.documents = documents
        self.parse_cache = parse_cache or AttachmentParseResultRepository(store)
        self.root = settings.song_agent_attachment_dir.expanduser().resolve()
        # 进程内按缓存键互斥，避免同一附件同一解析重复触发外部服务；
        # 数据库唯一约束仍是最终保障。
        self._locks: dict[tuple[str, str, str], asyncio.Lock] = {}

    async def analyze_image(
        self,
        access: AttachmentAccess,
        value: AnalyzeImageInput,
    ) -> AnalyzeImageResult:
        attachment = await self._claim(value.attachment_id, access, "image")
        tool_name = "attachments.analyze_image"
        normalized = normalize_instruction(tool_name, value.instruction)
        try:
            cached = await self._find_cached(
                attachment, access, tool_name, normalized
            )
            if cached is not None:
                await self._finish(attachment.attachment_id, True)
                return await self._rebuild_image(attachment, access, cached.result_ref)
            async with self._lock_for(attachment.attachment_id, tool_name, normalized):
                cached = await self._find_cached(
                    attachment, access, tool_name, normalized
                )
                if cached is not None:
                    await self._finish(attachment.attachment_id, True)
                    return await self._rebuild_image(
                        attachment, access, cached.result_ref
                    )
                payload = await self.vision.analyze(
                    ensure_within_root(attachment.storage_path, self.root),
                    attachment.media_type,
                    value.instruction,
                )
                result_ref = await self._save(
                    access,
                    tool_name,
                    payload,
                    "图片已解析",
                )
                await self._cache_put(
                    attachment, access, tool_name, normalized, result_ref
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
        tool_name = "attachments.transcribe_audio"
        normalized = normalize_instruction(
            tool_name, None, language=value.language
        )
        try:
            cached = await self._find_cached(
                attachment, access, tool_name, normalized
            )
            if cached is not None:
                await self._finish(attachment.attachment_id, True)
                return await self._rebuild_transcript(
                    attachment, access, cached.result_ref
                )
            async with self._lock_for(attachment.attachment_id, tool_name, normalized):
                cached = await self._find_cached(
                    attachment, access, tool_name, normalized
                )
                if cached is not None:
                    await self._finish(attachment.attachment_id, True)
                    return await self._rebuild_transcript(
                        attachment, access, cached.result_ref
                    )
                payload = await self.asr.transcribe(
                    ensure_within_root(attachment.storage_path, self.root),
                    filename=attachment.filename,
                    media_type=attachment.media_type,
                    language=value.language,
                )
                result_ref = await self._save(
                    access,
                    tool_name,
                    payload,
                    "语音已转写",
                )
                await self._cache_put(
                    attachment, access, tool_name, normalized, result_ref
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
        tool_name = "attachments.parse_document"
        normalized = normalize_instruction(tool_name, value.instruction)
        try:
            cached = await self._find_cached(
                attachment, access, tool_name, normalized
            )
            if cached is not None:
                await self._finish(attachment.attachment_id, True)
                return await self._rebuild_document(
                    attachment, access, cached.result_ref, value.instruction
                )
            async with self._lock_for(attachment.attachment_id, tool_name, normalized):
                cached = await self._find_cached(
                    attachment, access, tool_name, normalized
                )
                if cached is not None:
                    await self._finish(attachment.attachment_id, True)
                    return await self._rebuild_document(
                        attachment, access, cached.result_ref, value.instruction
                    )
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
                    tool_name,
                    full_payload,
                    summary,
                    truncated=truncated,
                )
                await self._cache_put(
                    attachment, access, tool_name, normalized, result_ref
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

    async def _find_cached(
        self,
        attachment: Attachment,
        access: AttachmentAccess,
        tool_name: str,
        normalized: str,
    ):
        key = build_key(attachment.attachment_id, tool_name, normalized)
        return await self.parse_cache.find(key, access)

    async def _cache_put(
        self,
        attachment: Attachment,
        access: AttachmentAccess,
        tool_name: str,
        normalized: str,
        result_ref: str,
    ) -> None:
        key = build_key(attachment.attachment_id, tool_name, normalized)
        now = int(time.time())
        ttl = self.settings.song_agent_attachment_ttl_seconds
        expires_at = min(
            attachment.expires_at if attachment.expires_at is not None else now + ttl,
            now + ttl,
        )
        await self.parse_cache.save(key, result_ref, expires_at, access)

    def _lock_for(
        self,
        attachment_id: str,
        tool_name: str,
        normalized: str,
    ) -> asyncio.Lock:
        lock_key = (attachment_id, tool_name, normalized)
        lock = self._locks.get(lock_key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[lock_key] = lock
        return lock

    async def _load_payload(
        self,
        access: AttachmentAccess,
        result_ref: str,
    ) -> dict[str, Any]:
        stored = await self.store.get_tool_result(
            result_ref,
            tenant_key=access.tenant_key,
            app_id=access.app_id,
            principal_id=access.principal_id,
        )
        if stored is None:
            raise RuntimeError("解析结果已过期或无权访问")
        return stored["payload"]

    async def _rebuild_image(
        self,
        attachment: Attachment,
        access: AttachmentAccess,
        result_ref: str,
    ) -> AnalyzeImageResult:
        payload = await self._load_payload(access, result_ref)
        return AnalyzeImageResult(
            attachment_id=attachment.attachment_id,
            result_ref=result_ref,
            **payload,
        )

    async def _rebuild_transcript(
        self,
        attachment: Attachment,
        access: AttachmentAccess,
        result_ref: str,
    ) -> TranscribeAudioResult:
        payload = await self._load_payload(access, result_ref)
        return TranscribeAudioResult(
            attachment_id=attachment.attachment_id,
            result_ref=result_ref,
            **payload,
        )

    async def _rebuild_document(
        self,
        attachment: Attachment,
        access: AttachmentAccess,
        result_ref: str,
        instruction: str,
    ) -> ParseDocumentResult:
        payload = await self._load_payload(access, result_ref)
        preview_limit = self.settings.song_agent_document_max_preview_chars
        context_limit = self.settings.song_agent_document_max_context_chars
        content = str(payload.get("content") or "")
        filename = str(payload.get("filename") or attachment.filename)
        title = payload.get("title")
        _, outline = _document_structure(content, filename)
        summary = _summary(content, instruction, context_limit)
        truncated = len(content) > preview_limit
        return ParseDocumentResult(
            attachment_id=attachment.attachment_id,
            filename=filename,
            media_type=str(payload.get("media_type") or attachment.media_type),
            title=title,
            summary=summary,
            outline=outline,
            content_preview=content[:preview_limit],
            result_ref=result_ref,
            truncated=truncated,
            metadata=payload.get("metadata") or {},
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
