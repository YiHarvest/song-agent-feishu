from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import httpx
from openai import APIConnectionError, APITimeoutError, AsyncOpenAI, RateLimitError

from ..config import Settings


class VisionClient:
    # 升级模型/服务/解析 Prompt/输出结构时必须 bump；超时/重试/日志不需要。
    processor_version = "vision-v1"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.logger = logging.getLogger(__name__)
        key = settings.song_agent_vision_api_key
        timeout = httpx.Timeout(
            connect=settings.song_agent_vision_connect_timeout_seconds,
            read=settings.song_agent_vision_read_timeout_seconds,
            write=30,
            pool=10,
        )
        self.client = (
            AsyncOpenAI(
                api_key=key.get_secret_value(),
                base_url=str(settings.song_agent_vision_base_url).rstrip("/"),
                max_retries=settings.song_agent_vision_max_retries,
                timeout=timeout,
                http_client=httpx.AsyncClient(
                    timeout=timeout,
                    trust_env=False,
                ),
            )
            if key
            else None
        )

    async def close(self) -> None:
        if self.client is not None:
            await self.client.close()

    async def analyze(self, path: Path, media_type: str, instruction: str) -> dict[str, Any]:
        if not self.settings.song_agent_vision_enabled or self.client is None:
            raise RuntimeError("图片理解服务未启用")
        raw_image = await asyncio.to_thread(path.read_bytes)
        encoded = base64.b64encode(raw_image).decode("ascii")
        started = time.monotonic()
        self.logger.info(
            "🖼️ 图片理解开始 model=%s bytes=%d thinking=%s timeout=%ss",
            self.settings.song_agent_vision_model,
            len(raw_image),
            self.settings.song_agent_vision_thinking_enabled,
            self.settings.song_agent_vision_read_timeout_seconds,
        )
        try:
            response = await self.client.chat.completions.create(
                model=self.settings.song_agent_vision_model,
                max_tokens=self.settings.song_agent_vision_max_tokens,
                response_format={"type": "json_object"},
                extra_body={
                    "thinking": {
                        "type": (
                            "enabled"
                            if self.settings.song_agent_vision_thinking_enabled
                            else "disabled"
                        )
                    }
                },
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是图片理解工具。只返回简洁 JSON，字段必须为 image_type、"
                            "description、visible_text、analysis、confidence。"
                            "visible_text 只保留关键文字，最多 8 项，每项最多 80 字。"
                            "analysis 控制在 300 字以内，必须以完整句子结束，不得返回 Markdown。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": instruction},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:{media_type};base64,{encoded}"},
                            },
                        ],
                    },
                ],
            )
        except APITimeoutError as error:
            elapsed = time.monotonic() - started
            self.logger.warning(
                "图片理解超时 model=%s elapsed_ms=%d retries=%d",
                self.settings.song_agent_vision_model,
                round(elapsed * 1000),
                self.settings.song_agent_vision_max_retries,
            )
            raise VisionTimeoutError("图片理解服务响应超时") from error
        except RateLimitError as error:
            elapsed = time.monotonic() - started
            self.logger.warning(
                "图片理解服务繁忙 model=%s elapsed_ms=%d status=%s",
                self.settings.song_agent_vision_model,
                round(elapsed * 1000),
                getattr(error, "status_code", 429),
            )
            raise VisionBusyError("图片理解服务当前繁忙") from error
        except APIConnectionError as error:
            elapsed = time.monotonic() - started
            self.logger.warning(
                "图片理解连接失败 model=%s elapsed_ms=%d",
                self.settings.song_agent_vision_model,
                round(elapsed * 1000),
            )
            raise VisionConnectionError("无法连接图片理解服务") from error
        choice = response.choices[0]
        finish_reason = str(getattr(choice, "finish_reason", "") or "")
        self.logger.info(
            "✅ 图片理解完成 model=%s elapsed_ms=%d finish_reason=%s",
            self.settings.song_agent_vision_model,
            round((time.monotonic() - started) * 1000),
            finish_reason or "unknown",
        )
        if finish_reason in {"length", "max_tokens"}:
            self.logger.warning(
                "图片理解输出达到长度上限 model=%s max_tokens=%d",
                self.settings.song_agent_vision_model,
                self.settings.song_agent_vision_max_tokens,
            )
        raw = choice.message.content or ""
        return _safe_json(raw)


def _safe_json(raw: str) -> dict[str, Any]:
    candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.IGNORECASE)
    try:
        value = json.loads(candidate)
        if isinstance(value, dict):
            visible_text = value.get("visible_text")
            if not isinstance(visible_text, list):
                visible_text = []
            confidence = value.get("confidence")
            if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
                confidence = None
            return {
                "image_type": str(value.get("image_type") or "other"),
                "description": str(value.get("description") or ""),
                "visible_text": [str(item) for item in visible_text if isinstance(item, str)],
                "analysis": _remove_dangling_clause(
                    str(value.get("analysis") or value.get("description") or raw)
                ),
                "confidence": confidence,
            }
    except (json.JSONDecodeError, TypeError):
        pass
    return {
        "image_type": "other",
        "description": raw[:2000],
        "visible_text": [],
        "analysis": _remove_dangling_clause(raw[:4000]),
        "confidence": None,
    }


def _remove_dangling_clause(text: str) -> str:
    """删除模型在完整句后偶发留下的半句，不裁剪正常短答。"""

    value = text.strip()
    if not value or value[-1] in "。！？.!?；;：:":
        return value
    last_end = max(value.rfind(mark) for mark in "。！？.!?")
    if last_end < 0:
        return value
    tail = value[last_end + 1 :].strip()
    dangling_prefixes = (
        "此外",
        "同时",
        "另外",
        "但是",
        "但",
        "并且",
        "以及",
        "需注意",
        "需要注意",
        "值得注意",
    )
    if len(tail) <= 30 and tail.startswith(dangling_prefixes):
        return value[: last_end + 1]
    return value


class VisionTimeoutError(TimeoutError):
    """图片已读取，但上游视觉模型未在配置时间内响应。"""


class VisionBusyError(RuntimeError):
    """上游视觉模型暂时过载或触发容量限制。"""


class VisionConnectionError(ConnectionError):
    """上游视觉模型当前无法连接。"""
