from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

import httpx
from openai import AsyncOpenAI

from ..kernel.models import AttachmentPart, ContentPart, TextPart
from ..ports.llm import (
    AgentDecision,
    AgentToolDescriptor,
    IntentDescriptor,
    RouteDecision,
)


class OpenAIModelAdapter:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        connect_timeout: float,
        read_timeout: float,
        max_retries: int,
    ) -> None:
        self.model = model
        self.client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=httpx.Timeout(
                connect=connect_timeout,
                read=read_timeout,
                write=read_timeout,
                pool=connect_timeout,
            ),
            max_retries=max_retries,
        )

    async def route(
        self,
        content: tuple[ContentPart, ...],
        intents: tuple[IntentDescriptor, ...],
        context: dict[str, Any],
        *,
        repair_instruction: str = "",
    ) -> RouteDecision:
        catalog = [
            {
                "intent_id": item.intent_id,
                "description": item.description,
                "arguments_schema": item.arguments_schema,
                "examples": item.examples,
            }
            for item in intents
        ]
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Select exactly one top-level intent from the supplied catalog. "
                        "Return JSON with intent_id, arguments, confidence, missing_fields. "
                        "Never invent an intent.\n"
                        f"CATALOG={json.dumps(catalog, ensure_ascii=False)}\n"
                        f"CONTEXT={json.dumps(context, ensure_ascii=False, default=str)}\n"
                        f"REPAIR={repair_instruction}"
                    ),
                },
                {"role": "user", "content": _content_text(content)},
            ],
            response_format={"type": "json_object"},
        )
        return RouteDecision.model_validate_json(response.choices[0].message.content or "{}")

    async def respond(
        self,
        text: str,
        context: dict[str, Any],
        *,
        system_instruction: str = "",
    ) -> str:
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        (system_instruction or "Answer helpfully and accurately.")
                        + "\nContext: "
                        + json.dumps(context, ensure_ascii=False, default=str)
                    ),
                },
                {"role": "user", "content": text},
            ],
        )
        return response.choices[0].message.content or ""

    async def decide(
        self,
        *,
        user_text: str,
        context: dict[str, Any],
        observations: tuple[dict[str, Any], ...],
        tools: tuple[AgentToolDescriptor, ...],
    ) -> AgentDecision:
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Plan one step at a time. Return JSON only. The shape is either "
                        '{"kind":"intent_call","intent_id":"...","arguments":{}}, '
                        '{"kind":"ask_user","content":"..."}, or '
                        '{"kind":"final_answer","content":"..."}. '
                        "Use only a listed intent and never call agent.execute.\n"
                        f"TOOLS={json.dumps([asdict(tool) for tool in tools], ensure_ascii=False)}\n"
                        f"CONTEXT={json.dumps(context, ensure_ascii=False, default=str)}\n"
                        f"OBSERVATIONS={json.dumps(observations, ensure_ascii=False, default=str)}"
                    ),
                },
                {"role": "user", "content": user_text},
            ],
            response_format={"type": "json_object"},
        )
        payload = json.loads(response.choices[0].message.content or "{}")
        return AgentDecision(
            kind=payload.get("kind", "ask_user"),
            content=str(payload.get("content", "")),
            intent_id=str(payload.get("intent_id", "")),
            arguments=payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {},
        )

    async def close(self) -> None:
        await self.client.close()


def _content_text(content: tuple[ContentPart, ...]) -> str:
    values: list[str] = []
    for part in content:
        if isinstance(part, TextPart):
            values.append(part.text)
        elif isinstance(part, AttachmentPart):
            attachment = part.attachment
            values.append(
                f"[attachment id={attachment.attachment_id} mime={attachment.media_type} "
                f"filename={attachment.filename}]"
            )
    return "\n".join(values)
