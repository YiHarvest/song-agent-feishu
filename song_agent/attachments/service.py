from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..config import Settings
from ..feishu.media import FeishuMediaDownloader
from ..models import IncomingMessage
from .models import (
    AnalyzeImageInput,
    Attachment,
    AttachmentAccess,
    ParseDocumentInput,
    TranscribeAudioInput,
)
from .repository import AttachmentRepository
from .storage import (
    UnsafeAttachmentError,
    atomic_move,
    ensure_private_directory,
    inspect_file,
)
from .tools import AttachmentTools


class PreparedAttachmentMessage(BaseModel):
    text: str
    context: dict[str, Any] = Field(default_factory=dict)
    direct_response: str | None = None


class AttachmentService:
    def __init__(
        self,
        settings: Settings,
        downloader: FeishuMediaDownloader,
        repository: AttachmentRepository,
        tools: AttachmentTools,
    ) -> None:
        self.settings = settings
        self.downloader = downloader
        self.repository = repository
        self.tools = tools
        self.root = settings.song_agent_attachment_dir.expanduser().resolve()
        self.temp_root = settings.song_agent_attachment_temp_dir.expanduser().resolve()
        self.logger = logging.getLogger(__name__)

    async def initialize(self) -> None:
        ensure_private_directory(self.root)
        ensure_private_directory(self.temp_root)
        recovered = await self.repository.recover_interrupted()
        if recovered:
            self.logger.warning(
                "已恢复 %d 个被进程重启中断的附件任务",
                recovered,
            )

    async def prepare(self, message: IncomingMessage, user_text: str) -> PreparedAttachmentMessage:
        if not self.settings.song_agent_attachments_enabled:
            raise RuntimeError("附件功能未启用")
        if not message.attachments:
            return PreparedAttachmentMessage(text=user_text)
        if len(message.attachments) > self.settings.song_agent_attachment_max_files_per_message:
            raise ValueError("单条消息附件数量超过限制")
        image_count = sum(item.kind == "image" for item in message.attachments)
        if image_count > self.settings.song_agent_vision_max_images_per_message:
            raise ValueError("单条消息图片数量超过限制")
        access = AttachmentAccess(
            tenant_key=message.tenant_key,
            app_id=message.app_id or self.settings.feishu_app_id,
            principal_id=message.user_id,
        )
        downloaded: list[Attachment] = []
        total = 0
        total_limit = self.settings.song_agent_attachment_max_total_mb_per_message * 1024 * 1024
        for reference in message.attachments:
            attachment = await self._download(
                message,
                access,
                reference,
                remaining_total_bytes=total_limit - total,
            )
            total += attachment.size_bytes
            downloaded.append(attachment)

        retrieved: list[dict[str, Any]] = []
        transcripts: list[str] = []
        image_answers: list[str] = []
        for attachment in downloaded:
            if attachment.attachment_kind == "image":
                instruction = (
                    f"分析这张图片，重点回答用户的问题：“{user_text}”；同时提取关键错误文字。"
                    if user_text
                    else "描述图片内容，提取明显文字，并指出可能需要用户关注的信息。"
                )
                result = await self.tools.analyze_image(
                    access,
                    AnalyzeImageInput(
                        attachment_id=attachment.attachment_id,
                        instruction=instruction,
                    ),
                )
                payload = result.model_dump(mode="json")
                retrieved.append(payload)
                answer = (result.analysis or result.description).strip()
                if answer:
                    image_answers.append(answer)
            elif attachment.attachment_kind == "audio":
                result = await self.tools.transcribe_audio(
                    access,
                    TranscribeAudioInput(
                        attachment_id=attachment.attachment_id,
                        language=self.settings.song_agent_asr_default_language,
                    ),
                )
                transcripts.append(result.text)
                retrieved.append(
                    {
                        "source_type": "audio",
                        "attachment_id": result.attachment_id,
                        "asr_task_id": result.task_id,
                        "result_ref": result.result_ref,
                        "transcript": result.text,
                    }
                )
            elif attachment.attachment_kind == "document":
                result = await self.tools.parse_document(
                    access,
                    ParseDocumentInput(
                        attachment_id=attachment.attachment_id,
                        instruction=user_text or "总结文件内容并提取关键结论。",
                    ),
                )
                retrieved.append(result.model_dump(mode="json"))
            else:
                raise UnsafeAttachmentError("不支持的附件类型")
        exact_text = "\n".join(text.strip() for text in transcripts if text.strip())
        if exact_text:
            routed_text = exact_text
        else:
            routed_text = user_text or "请分析我发送的附件。"
        direct_response = None
        kinds = {attachment.attachment_kind for attachment in downloaded}
        if kinds == {"image"} and image_answers and _is_direct_image_question(user_text):
            direct_response = "\n\n".join(image_answers)
        elif (
            kinds == {"audio"}
            and not user_text
            and exact_text
            and _is_transcription_confirmation(exact_text)
        ):
            direct_response = f"听到了。你说的是：**{exact_text}**"
        return PreparedAttachmentMessage(
            text=routed_text,
            context={
                "source_type": "attachment",
                "attachment_kinds": sorted(kinds),
                "retrieved": retrieved,
                "retrieved_context": retrieved,
            },
            direct_response=direct_response,
        )

    async def _download(
        self,
        message,
        access,
        reference,
        *,
        remaining_total_bytes: int,
    ) -> Attachment:
        suffix = Path(reference.filename).suffix.lower()[:12]
        temp_path = self.temp_root / f"{uuid.uuid4().hex}{suffix}"
        placeholder = self.root / f"{uuid.uuid4().hex}{suffix}"
        attachment = await self.repository.create(
            access=access,
            source_message_id=message.message_id,
            source_resource_key=reference.resource_key,
            kind=reference.kind,
            filename=reference.filename or f"attachment{suffix}",
            storage_path=placeholder,
            ttl_seconds=self.settings.song_agent_attachment_ttl_seconds,
        )
        kind_limits = {
            "image": self.settings.song_agent_vision_max_image_mb,
            "audio": self.settings.song_agent_asr_max_audio_mb,
            "document": self.settings.song_agent_document_max_file_mb,
            "unknown": 1,
        }
        try:
            remote_name = await self.downloader.download(
                message_id=message.message_id,
                resource_key=reference.resource_key,
                destination=temp_path,
                resource_type=reference.resource_type,
                max_bytes=min(
                    kind_limits[reference.kind] * 1024 * 1024,
                    remaining_total_bytes,
                ),
            )
            filename = reference.filename or remote_name or "attachment"
            media_type, size, sha256 = inspect_file(
                temp_path,
                declared_kind=reference.kind,
                filename=filename,
            )
            destination = self.root / f"{attachment.attachment_id}{suffix}"
            atomic_move(temp_path, destination, self.root)
            await self.repository.update_ready(
                attachment.attachment_id,
                storage_path=destination,
                filename=filename,
                media_type=media_type,
                size_bytes=size,
                sha256=sha256,
            )
            ready = await self.repository.get_owned(attachment.attachment_id, access)
            if ready is None:
                raise RuntimeError("附件保存失败")
            return ready
        except Exception:
            temp_path.unlink(missing_ok=True)
            await self.repository.transition(
                attachment.attachment_id,
                from_statuses=("downloading", "ready"),
                to_status="failed",
            )
            raise


def _is_direct_image_question(text: str) -> bool:
    normalized = "".join(text.lower().split())
    if not normalized:
        return True
    operational_markers = (
        "创建",
        "新建",
        "添加",
        "安排",
        "提醒",
        "日程",
        "任务",
        "保存",
        "发送",
        "写入",
    )
    if any(marker in normalized for marker in operational_markers):
        return False
    return bool(
        any(marker in normalized for marker in ("这是什么", "那是什么", "什么内容"))
        or re.search(r"(图片|图里|图中|照片|截图).*(什么|内容|显示|写了|错误|问题)", normalized)
        or re.match(r"^(识别|分析|描述|看看|看下|提取|读取)", normalized)
    )


def _is_transcription_confirmation(text: str) -> bool:
    normalized = "".join(text.lower().split())
    return bool(
        re.search(
            r"(能|可以).{0,4}(听见|听到|听清|听懂|听得到|识别)",
            normalized,
        )
        or re.search(r"(听见|听到|听清|听懂|听得到).{0,4}(吗|没|没有)", normalized)
        or re.search(r"(我|刚才).{0,4}(说了|说的|说).{0,4}(什么|啥)", normalized)
        or "听到了吗" in normalized
    )
