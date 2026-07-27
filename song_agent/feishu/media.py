from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from lark_oapi.api.im.v1 import GetMessageResourceRequest


class FeishuMediaDownloader:
    """飞书消息资源下载边界；资源键不会离开本模块和附件服务。"""

    def __init__(self, client: Any, *, timeout_seconds: float) -> None:
        self.client = client
        self.timeout_seconds = timeout_seconds

    async def download(
        self,
        *,
        message_id: str,
        resource_key: str,
        destination: Path,
        resource_type: str,
        max_bytes: int,
    ) -> str:
        request = (
            GetMessageResourceRequest.builder()
            .message_id(message_id)
            .file_key(resource_key)
            .type(resource_type)
            .build()
        )
        response = await asyncio.wait_for(
            asyncio.to_thread(self.client.im.v1.message_resource.get, request),
            timeout=self.timeout_seconds,
        )
        if not response.success() or response.file is None:
            if response.code == 99991672:
                raise FeishuMediaPermissionError(
                    "飞书应用缺少 im:message:readonly 权限"
                )
            raise RuntimeError(f"飞书附件下载失败，错误码：{response.code}")
        try:
            await asyncio.to_thread(
                _write_response_file,
                response.file,
                destination,
                max_bytes,
            )
        except Exception:
            await asyncio.to_thread(destination.unlink, missing_ok=True)
            raise
        return response.file_name or ""


def _write_response_file(source: Any, destination: Path, max_bytes: int) -> int:
    written = 0
    with destination.open("xb") as output:
        while chunk := source.read(1024 * 1024):
            written += len(chunk)
            if written > max_bytes:
                raise ValueError("附件超过大小限制")
            output.write(chunk)
    return written


class FeishuMediaPermissionError(PermissionError):
    """飞书应用尚未开通读取消息资源所需权限。"""
