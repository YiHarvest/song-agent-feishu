from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel

from ..config import Settings


class _AsrResponse(BaseModel):
    text: str
    language: str
    task_id: str


class AsrClient:
    # 升级模型/服务/解析 Prompt/输出结构时必须 bump；超时/重试/日志不需要。
    processor_version = "asr-v1"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = httpx.AsyncClient(
            base_url=str(settings.song_agent_asr_base_url).rstrip("/"),
            timeout=httpx.Timeout(
                connect=settings.song_agent_asr_connect_timeout_seconds,
                read=settings.song_agent_asr_read_timeout_seconds,
                write=60,
                pool=10,
            ),
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def transcribe(
        self,
        path: Path,
        *,
        filename: str,
        media_type: str,
        language: str,
    ) -> dict[str, Any]:
        if not self.settings.song_agent_asr_enabled:
            raise RuntimeError("语音识别服务未启用")
        with path.open("rb") as stream:
            response = await self.client.post(
                self.settings.song_agent_asr_path,
                params={"language": language},
                files={"file": (filename, stream, media_type)},
            )
        response.raise_for_status()
        return _AsrResponse.model_validate(response.json()).model_dump()
