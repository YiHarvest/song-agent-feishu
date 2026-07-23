"""
LLM 调用模块。

封装 OpenAI 兼容的 Chat Completions API，支持结构化输出。
"""

from __future__ import annotations

import json
import logging
import re
from typing import TypeVar

import httpx
from pydantic import BaseModel

from .config import Settings

T = TypeVar("T", bound=BaseModel)


class StructuredLlm:
    """
    结构化 LLM 调用器。

    封装 OpenAI 兼容的 Chat Completions API，支持 JSON 结构化输出。
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.logger = logging.getLogger(__name__)

    async def generate(self, schema: type[T], system: str, user: str) -> T:
        self.logger.info(
            "🧠 LLM 开始思考 model=%s output=%s input=%r",
            self.settings.llm_model,
            schema.__name__,
            user,
        )
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{self.settings.llm_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.settings.llm_api_key}"},
                json={
                    "model": self.settings.llm_model,
                    "temperature": 0,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                },
            )
        if response.is_error:
            try:
                message = response.json().get("error", {}).get("message")
            except (ValueError, AttributeError):
                message = None
            raise RuntimeError(message or f"模型请求失败: HTTP {response.status_code}")
        payload = response.json()
        raw = payload.get("choices", [{}])[0].get("message", {}).get("content")
        if isinstance(raw, list):
            raw = "".join(item.get("text", "") for item in raw if isinstance(item, dict))
        if not isinstance(raw, str) or not raw.strip():
            raise RuntimeError("模型返回了空内容")
        result = schema.model_validate(json.loads(extract_json(raw)))
        self.logger.info("🧠 LLM 思考完成 output=%s result=%s", schema.__name__, result.model_dump_json())
        return result


def extract_json(content: str) -> str:
    trimmed = re.sub(r"^```(?:json)?\s*", "", content.strip(), flags=re.IGNORECASE)
    trimmed = re.sub(r"\s*```$", "", trimmed)
    start, end = trimmed.find("{"), trimmed.rfind("}")
    if start < 0 or end < start:
        raise RuntimeError("模型返回内容不是 JSON")
    return trimmed[start : end + 1]
