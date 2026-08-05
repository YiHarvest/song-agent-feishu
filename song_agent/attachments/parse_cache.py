"""附件解析结果缓存（Q4b）。

缓存键 = (attachment_id, tool_name, instruction_hash, processor_version)。
- 不按 sha256 跨消息复用，缓存只命中同一个 attachment_id。
- 缓存键必须包含显式 processor_version，升级解析模型/服务/prompt/输出
  结构时必须 bump，否则会静默返回旧模型结果。
- TTL 固定为创建时附件剩余过期时间与配置 TTL 的较小值；命中不刷新。
"""

from __future__ import annotations

import hashlib
import re
import time
import unicodedata
import uuid
from dataclasses import dataclass

from ..store import SqliteStore
from .models import AttachmentAccess

# 处理器版本：升级模型/服务/核心解析 Prompt/输出结构时必须 bump；
# 仅改超时、重试、日志、连接池不需要 bump。定义在 Integration 客户端类属性上。
VISION_PROCESSOR_VERSION = "vision-v1"
ASR_PROCESSOR_VERSION = "asr-v1"
DOCUMENT_PROCESSOR_VERSION = "mineru-v1"

_TOOL_PROCESSOR_VERSION = {
    "attachments.analyze_image": VISION_PROCESSOR_VERSION,
    "attachments.transcribe_audio": ASR_PROCESSOR_VERSION,
    "attachments.parse_document": DOCUMENT_PROCESSOR_VERSION,
}

_ASR_TOOL = "attachments.transcribe_audio"
_IMAGE_TOOL = "attachments.analyze_image"
_DEFAULT_INSTRUCTION = "__default__"


def processor_version_for(tool_name: str) -> str:
    try:
        return _TOOL_PROCESSOR_VERSION[tool_name]
    except KeyError as exc:
        raise ValueError(f"no processor version for tool: {tool_name}") from exc


def normalize_instruction(
    tool_name: str,
    instruction: str | None,
    *,
    language: str | None = None,
) -> str:
    """按工具类型归一化解析指令，生成稳定的缓存键输入。

    - ASR：语言参与缓存键（auto ≠ zh），无语言时使用明确值 auto。
    - Vision：strip + Unicode NFKC + 合并连续空白 + 英文小写；不删标点。
    - MinerU：无显式指令时使用固定占位 __default__。
    """
    if tool_name == _ASR_TOOL:
        return f"language={language or 'auto'}"
    value = (instruction or "").strip()
    if not value:
        return _DEFAULT_INSTRUCTION
    value = unicodedata.normalize("NFKC", value)
    value = re.sub(r"\s+", " ", value)
    if tool_name == _IMAGE_TOOL:
        value = value.lower()
    return value


def instruction_hash(normalized: str) -> str:
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AttachmentParseKey:
    attachment_id: str
    tool_name: str
    instruction_hash: str
    processor_version: str


@dataclass(frozen=True, slots=True)
class AttachmentParseResult:
    id: str
    attachment_id: str
    tool_name: str
    instruction_hash: str
    processor_version: str
    result_ref: str
    created_at: int
    expires_at: int


def build_key(
    attachment_id: str,
    tool_name: str,
    normalized_instruction: str,
) -> AttachmentParseKey:
    return AttachmentParseKey(
        attachment_id=attachment_id,
        tool_name=tool_name,
        instruction_hash=instruction_hash(normalized_instruction),
        processor_version=processor_version_for(tool_name),
    )


class AttachmentParseResultRepository:
    """attachment_parse_results 表的访问层。

    所有查询通过关联 attachments 表校验
    tenant_key / app_id / principal_id / attachment_id 所有权。
    """

    def __init__(self, store: SqliteStore) -> None:
        self.store = store

    async def find(
        self,
        key: AttachmentParseKey,
        access: AttachmentAccess,
    ) -> AttachmentParseResult | None:
        row = await (
            await self.store.db.execute(
                """
                SELECT p.id, p.attachment_id, p.tool_name, p.instruction_hash,
                       p.processor_version, p.result_ref, p.created_at, p.expires_at
                FROM attachment_parse_results p
                JOIN attachments a ON a.attachment_id = p.attachment_id
                WHERE p.attachment_id = ? AND p.tool_name = ?
                  AND p.instruction_hash = ? AND p.processor_version = ?
                  AND p.expires_at > ?
                  AND a.tenant_key = ? AND a.app_id = ? AND a.principal_id = ?
                  AND a.status != 'expired'
                  AND (a.expires_at IS NULL OR a.expires_at > ?)
                """,
                (
                    key.attachment_id,
                    key.tool_name,
                    key.instruction_hash,
                    key.processor_version,
                    int(time.time()),
                    access.tenant_key,
                    access.app_id,
                    access.principal_id,
                    int(time.time()),
                ),
            )
        ).fetchone()
        if row is None:
            return None
        return AttachmentParseResult(
            id=row["id"],
            attachment_id=row["attachment_id"],
            tool_name=row["tool_name"],
            instruction_hash=row["instruction_hash"],
            processor_version=row["processor_version"],
            result_ref=row["result_ref"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
        )

    async def save(
        self,
        key: AttachmentParseKey,
        result_ref: str,
        expires_at: int,
        access: AttachmentAccess,
    ) -> AttachmentParseResult:
        """写入缓存映射；重复写入按唯一键更新为最新结果。"""
        now = int(time.time())
        row_id = str(uuid.uuid4())
        await self.store.db.execute(
            """
            INSERT INTO attachment_parse_results(
                id, attachment_id, tool_name, instruction_hash, processor_version,
                result_ref, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(attachment_id, tool_name, instruction_hash, processor_version)
            DO UPDATE SET result_ref = excluded.result_ref, expires_at = excluded.expires_at
            """,
            (
                row_id,
                key.attachment_id,
                key.tool_name,
                key.instruction_hash,
                key.processor_version,
                result_ref,
                now,
                expires_at,
            ),
        )
        await self.store.db.commit()
        return AttachmentParseResult(
            id=row_id,
            attachment_id=key.attachment_id,
            tool_name=key.tool_name,
            instruction_hash=key.instruction_hash,
            processor_version=key.processor_version,
            result_ref=result_ref,
            created_at=now,
            expires_at=expires_at,
        )

    async def delete_expired(self, now: int | None = None) -> int:
        cursor = await self.store.db.execute(
            "DELETE FROM attachment_parse_results WHERE expires_at <= ?",
            (int(time.time()) if now is None else now,),
        )
        await self.store.db.commit()
        return cursor.rowcount
