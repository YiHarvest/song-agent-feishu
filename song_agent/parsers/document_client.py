from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx
import pypdfium2 as pdfium
from loguru import logger as loguru_logger
from mineru_vl_utils import MinerUClient
from PIL import Image

from ..config import Settings


class MinerUDocumentClient:
    """公司 MinerU VL 两阶段 PDF 解析客户端。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.logger = logging.getLogger(__name__)
        loguru_logger.disable("mineru_vl_utils")
        self._client: MinerUClient | None = None
        self._model_name = settings.song_agent_mineru_vl_model_name.strip()
        self._initialize_lock = asyncio.Lock()
        self._unavailable_reason = ""

    async def close(self) -> None:
        client = self._client
        self._client = None
        if client is None:
            return
        http_backend = getattr(client, "client", None)
        sync_client = getattr(http_backend, "_client", None)
        if sync_client is not None and hasattr(sync_client, "close"):
            await asyncio.to_thread(sync_client.close)

    async def parse(self, *, attachment_id: str, local_path: Path) -> tuple[str, dict[str, Any]]:
        if (
            not self.settings.song_agent_document_parser_enabled
            or self.settings.song_agent_document_parser_provider != "mineru_vl"
        ):
            raise RuntimeError("文档解析服务未启用")
        if local_path.suffix.lower() != ".pdf":
            raise RuntimeError("MinerU VL 当前只解析 PDF；文本文件走本地解析")
        max_bytes = self.settings.song_agent_document_max_file_mb * 1024 * 1024
        size_bytes = await asyncio.to_thread(lambda: local_path.stat().st_size)
        if size_bytes > max_bytes:
            raise RuntimeError("PDF 超过 MinerU VL 文件大小限制")

        client = await self._get_client()
        started = time.monotonic()
        self.logger.info(
            "MinerU VL 解析开始 attachment_id=%s model=%s",
            attachment_id,
            self._model_name,
        )
        try:
            async with asyncio.timeout(
                self.settings.song_agent_document_parse_timeout_seconds
            ):
                markdown, page_count = await self._parse_pdf(client, local_path)
        except TimeoutError as error:
            raise RuntimeError("MinerU VL 文档解析超时") from error
        if not markdown.strip():
            raise RuntimeError("MinerU VL 未返回文档内容")
        self.logger.info(
            "MinerU VL 解析完成 attachment_id=%s pages=%d elapsed_ms=%d",
            attachment_id,
            page_count,
            round((time.monotonic() - started) * 1000),
        )
        return markdown, {
            "provider": "mineru_vl",
            "model": self._model_name,
            "page_count": page_count,
        }

    async def _get_client(self) -> MinerUClient:
        if self._client is not None:
            return self._client
        if self._unavailable_reason:
            raise RuntimeError(self._unavailable_reason)
        async with self._initialize_lock:
            if self._client is not None:
                return self._client
            if self._unavailable_reason:
                raise RuntimeError(self._unavailable_reason)
            try:
                headers = _parse_headers(
                    self.settings.song_agent_mineru_vl_server_headers
                )
                if not self._model_name:
                    self._model_name = await self._discover_model(headers)
                self._client = await asyncio.to_thread(
                    MinerUClient,
                    backend="http-client",
                    server_url=str(
                        self.settings.song_agent_mineru_vl_base_url
                    ).rstrip("/"),
                    model_name=self._model_name,
                    server_headers=headers or None,
                    max_concurrency=self.settings.song_agent_mineru_vl_region_concurrency,
                    max_connections=self.settings.song_agent_mineru_vl_region_concurrency,
                    http_timeout=self.settings.song_agent_mineru_vl_read_timeout_seconds,
                    connect_timeout=self.settings.song_agent_mineru_vl_connect_timeout_seconds,
                    max_retries=self.settings.song_agent_mineru_vl_max_retries,
                    skip_model_name_checking=True,
                    use_tqdm=False,
                )
            except Exception as error:
                self._unavailable_reason = (
                    "MinerU VL 初始化失败；请检查公司接口和配置"
                )
                self.logger.warning(
                    "MinerU VL 初始化失败 error_type=%s",
                    type(error).__name__,
                )
                raise RuntimeError(self._unavailable_reason) from error
            self.logger.info(
                "MinerU VL 客户端已初始化 model=%s",
                self._model_name,
            )
            return self._client

    async def _discover_model(self, headers: dict[str, str]) -> str:
        timeout = httpx.Timeout(
            connect=self.settings.song_agent_mineru_vl_connect_timeout_seconds,
            read=self.settings.song_agent_mineru_vl_connect_timeout_seconds,
            write=10,
            pool=5,
        )
        async with httpx.AsyncClient(
            base_url=str(self.settings.song_agent_mineru_vl_base_url).rstrip("/"),
            headers=headers,
            timeout=timeout,
            trust_env=False,
        ) as client:
            response = await client.get("/models")
            response.raise_for_status()
        payload = response.json()
        models = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(models, list) or not models:
            raise RuntimeError("MinerU VL /models 未返回模型")
        first = models[0]
        model_name = first.get("id") if isinstance(first, dict) else None
        if not isinstance(model_name, str) or not model_name:
            raise RuntimeError("MinerU VL /models 缺少模型 ID")
        return model_name

    async def _parse_pdf(
        self,
        client: MinerUClient,
        local_path: Path,
    ) -> tuple[str, int]:
        page_count = await asyncio.to_thread(_pdf_page_count, local_path)
        if page_count <= 0:
            raise RuntimeError("PDF 无页面内容")
        if page_count > self.settings.song_agent_mineru_vl_max_pages:
            raise RuntimeError(
                f"PDF 共 {page_count} 页，超过限制 "
                f"{self.settings.song_agent_mineru_vl_max_pages} 页"
            )

        concurrency = self.settings.song_agent_mineru_vl_page_concurrency
        page_texts: list[str] = []
        for start in range(0, page_count, concurrency):
            page_indexes = list(range(start, min(start + concurrency, page_count)))
            images = await asyncio.to_thread(
                _render_pdf_batch,
                local_path,
                page_indexes,
                self.settings.song_agent_mineru_vl_pdf_scale,
            )
            try:
                extracted = await asyncio.gather(
                    *(
                        asyncio.to_thread(client.two_step_extract, image)
                        for image in images
                    )
                )
            finally:
                for image in images:
                    image.close()
            for page_index, blocks in zip(page_indexes, extracted, strict=True):
                content = _blocks_to_text(blocks)
                page_texts.append(
                    f"<!-- page:{page_index + 1} -->\n\n{content}".rstrip()
                )
        return "\n\n".join(page_texts), page_count


def _parse_headers(raw: str) -> dict[str, str]:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError as error:
        raise ValueError("SONG_AGENT_MINERU_VL_SERVER_HEADERS 不是有效 JSON") from error
    if not isinstance(value, dict):
        raise ValueError("SONG_AGENT_MINERU_VL_SERVER_HEADERS 必须是 JSON 对象")
    return {str(key): str(item) for key, item in value.items()}


def _pdf_page_count(path: Path) -> int:
    document = pdfium.PdfDocument(str(path))
    try:
        return len(document)
    finally:
        document.close()


def _render_pdf_batch(
    path: Path,
    page_indexes: list[int],
    scale: float,
) -> list[Image.Image]:
    document = pdfium.PdfDocument(str(path))
    images: list[Image.Image] = []
    try:
        for page_index in page_indexes:
            page = document.get_page(page_index)
            try:
                bitmap = page.render(scale=scale)
                try:
                    images.append(bitmap.to_pil().convert("RGB"))
                finally:
                    bitmap.close()
            finally:
                page.close()
    except Exception:
        for image in images:
            image.close()
        raise
    finally:
        document.close()
    return images


def _blocks_to_text(blocks: Any) -> str:
    chunks: list[str] = []
    for block in blocks:
        if isinstance(block, dict):
            content = block.get("content")
        else:
            content = getattr(block, "content", "")
        value = str(content or "").strip()
        if value:
            chunks.append(value)
    return "\n".join(chunks).strip()
