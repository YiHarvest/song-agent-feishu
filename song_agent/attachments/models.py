from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

AttachmentKind = Literal["image", "audio", "document", "unknown"]
AttachmentStatus = Literal["downloading", "ready", "parsing", "parsed", "failed", "expired"]


class AttachmentAccess(BaseModel):
    tenant_key: str
    app_id: str
    principal_id: str


class Attachment(BaseModel):
    attachment_id: str
    tenant_key: str
    app_id: str
    principal_id: str
    source: str
    source_message_id: str
    source_resource_key: str = ""
    attachment_kind: AttachmentKind
    filename: str
    media_type: str
    size_bytes: int
    sha256: str
    storage_path: Path
    status: AttachmentStatus
    created_at: int
    updated_at: int
    expires_at: int | None = None


class AnalyzeImageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attachment_id: str = Field(pattern=r"^att_[a-f0-9]{32}$")
    instruction: str = "描述图片内容，提取明显文字，并指出重要信息。"


class AnalyzeImageResult(BaseModel):
    attachment_id: str
    image_type: str
    description: str
    visible_text: list[str]
    analysis: str
    confidence: float | None = None
    result_ref: str


class TranscribeAudioInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attachment_id: str = Field(pattern=r"^att_[a-f0-9]{32}$")
    language: str = "auto"


class TranscribeAudioResult(BaseModel):
    attachment_id: str
    text: str
    language: str
    task_id: str
    result_ref: str


class ParseDocumentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attachment_id: str = Field(pattern=r"^att_[a-f0-9]{32}$")
    instruction: str = "总结文件内容并提取关键结论。"


class ParseDocumentResult(BaseModel):
    attachment_id: str
    filename: str
    media_type: str
    title: str | None = None
    summary: str
    outline: list[str]
    content_preview: str
    result_ref: str
    truncated: bool
    metadata: dict[str, Any] = Field(default_factory=dict)
