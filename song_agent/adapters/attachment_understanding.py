from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any

import httpx
from openai import AsyncOpenAI

from ..kernel.errors import DeliveryState, ProviderError


class OpenAIVisionProvider:
    def __init__(self, *, base_url: str, api_key: str, model: str) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.model = model

    async def analyze(
        self, path: Path, media_type: str, instruction: str
    ) -> dict[str, Any]:
        if not media_type.startswith("image/"):
            raise ProviderError(
                "attachment.not_an_image",
                "The attachment is not an image.",
                delivery_state=DeliveryState.NOT_SENT,
            )
        encoded = base64.b64encode(await asyncio.to_thread(path.read_bytes)).decode()
        try:
            async with AsyncOpenAI(base_url=self.base_url, api_key=self.api_key) as client:
                response = await client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": instruction or "Describe and extract the image content.",
                                },
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": f"data:{media_type};base64,{encoded}"
                                    },
                                },
                            ],
                        }
                    ],
                )
        except Exception as error:
            raise ProviderError(
                "attachment.vision_failed",
                "Image understanding provider failed.",
                retryable=True,
                delivery_state=DeliveryState.NOT_SENT,
            ) from error
        return {"text": response.choices[0].message.content or "", "media_type": media_type}


class HttpAsrProvider:
    def __init__(self, url: str, *, timeout: float = 300) -> None:
        self.url = url
        self.timeout = timeout

    async def transcribe(
        self,
        path: Path,
        *,
        filename: str,
        media_type: str,
        language: str,
    ) -> dict[str, Any]:
        return await _multipart_request(
            self.url,
            path,
            filename=filename,
            media_type=media_type,
            fields={"language": language},
            request_timeout=self.timeout,
            error_code="attachment.asr_failed",
        )


class HttpDocumentParserProvider:
    def __init__(self, url: str, *, timeout: float = 300) -> None:
        self.url = url
        self.timeout = timeout

    async def parse(
        self,
        path: Path,
        *,
        filename: str,
        media_type: str,
        instruction: str,
    ) -> dict[str, Any]:
        return await _multipart_request(
            self.url,
            path,
            filename=filename,
            media_type=media_type,
            fields={"instruction": instruction},
            request_timeout=self.timeout,
            error_code="attachment.document_parse_failed",
        )


async def _multipart_request(
    url: str,
    path: Path,
    *,
    filename: str,
    media_type: str,
    fields: dict[str, str],
    request_timeout: float,
    error_code: str,
) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=request_timeout, trust_env=False) as client:
            with path.open("rb") as stream:
                response = await client.post(
                    url,
                    data=fields,
                    files={"file": (filename, stream, media_type)},
                )
    except httpx.HTTPError as error:
        raise ProviderError(
            error_code,
            "Attachment understanding provider request failed.",
            retryable=True,
            delivery_state=DeliveryState.NOT_SENT,
        ) from error
    if response.is_error:
        raise ProviderError(
            error_code,
            f"Attachment understanding provider returned HTTP {response.status_code}.",
            retryable=response.status_code == 429 or response.status_code >= 500,
            delivery_state=DeliveryState.REJECTED,
        )
    payload = response.json()
    return payload if isinstance(payload, dict) else {"result": payload}
