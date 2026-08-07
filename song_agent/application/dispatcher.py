"""注册式意图分发器。

职责边界（Q3）：
- 意图提取
- 确定性业务分发（按注册的 handler）
- 缺失字段 / 低置信度澄清
- 开放式请求转交 Agent
- 会话记录编排（dispatch 与 resume 显式区分）

不负责：
- Agent 工具选择 / Tool Schema 裁剪 / 能力推断（归 ReActRuntime）
- 卡片回调（走 PendingActionModule）
- 命令层（归 FeishuChannel）
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from ..context.builders import BusinessContextBuilder
from ..context.service import ConversationContextService
from ..domain.intents import DETERMINISTIC_INTENTS, UserRequest
from ..domain.results import ApplicationResult
from ..intelligence.general_agent import GeneralAgent
from ..intelligence.intent_extractor import IntentExtractor
from ..llm import LLMTimeoutError
from .context.agent_input_builder import AgentInputBuilder

IntentHandler = Callable[
    [UserRequest, dict[str, Any]],
    Awaitable[ApplicationResult],
]


class ApplicationDispatcher:
    def __init__(
        self,
        intent_extractor: IntentExtractor,
        agent_runner: GeneralAgent,
        business_contexts: BusinessContextBuilder,
        conversation_contexts: ConversationContextService,
        agent_inputs: AgentInputBuilder,
        *,
        minimum_confidence: float = 0.65,
    ) -> None:
        self.intent_extractor = intent_extractor
        self.agent_runner = agent_runner
        self.business_contexts = business_contexts
        self.conversation_contexts = conversation_contexts
        self.agent_inputs = agent_inputs
        self.minimum_confidence = minimum_confidence
        self.handlers: dict[str, IntentHandler] = {}
        self.logger = logging.getLogger(__name__)

    def register(self, intent: str, handler: IntentHandler) -> None:
        if intent in self.handlers:
            raise ValueError(f"intent already registered: {intent}")
        self.handlers[intent] = handler

    async def dispatch(self, request: UserRequest) -> ApplicationResult:
        """普通消息入口：记录 user + 分发 + 记录 assistant。"""
        await self.conversation_contexts.record_user(request)
        result = await self._dispatch(request)
        await self.conversation_contexts.record_assistant(request, result.message)
        return result

    async def resume(
        self,
        request: UserRequest,
        *,
        authorization_id: str = "",
    ) -> ApplicationResult:
        """OAuth 恢复入口：不重复记录 user，assistant 使用独立 message_id。"""
        result = await self._dispatch(request)
        await self.conversation_contexts.record_assistant(
            request,
            result.message,
            message_id=f"assistant:oauth-resume:{authorization_id}" if authorization_id else "",
        )
        return result

    async def _dispatch(self, request: UserRequest) -> ApplicationResult:
        business_context = await self.business_contexts.build_for_intent_extraction(
            request
        )
        try:
            extracted = await self.intent_extractor.extract(
                request,
                business_context,
            )
        except LLMTimeoutError:
            return ApplicationResult(
                status="error",
                intent="conversation.general",
                message="模型服务响应超时，请稍后重试；无需重新授权。",
            )
        if extracted.confidence < self.minimum_confidence:
            return ApplicationResult(
                status="clarification_required",
                intent=extracted.intent,
                message="请求意图不够明确，请补充要操作的对象和时间。",
            )
        if extracted.missing_fields:
            return ApplicationResult(
                status="clarification_required",
                intent=extracted.intent,
                message="还需要：" + "、".join(extracted.missing_fields),
                data={"missing_fields": extracted.missing_fields},
            )
        handler = self.handlers.get(extracted.intent)
        if handler is not None:
            return await handler(request, extracted.arguments)
        if extracted.intent in DETERMINISTIC_INTENTS:
            return ApplicationResult(
                status="unsupported",
                intent=extracted.intent,
                message=f"{extracted.intent} 已进入确定性路由，但该业务服务尚未启用。",
            )
        # conversation.general 及未注册意图 → 开放式 Agent
        return await self._run_agent(request, business_context)

    async def _run_agent(
        self,
        request: UserRequest,
        business_context: Any,
    ) -> ApplicationResult:
        agent_metadata = self.agent_inputs.build_metadata(business_context)
        reference_context = _resolve_reference_context(request, business_context)
        if reference_context:
            agent_metadata["reference_context"] = reference_context
        document_context = _resolve_document_context(request, reference_context)
        if document_context:
            agent_metadata["document_context"] = document_context
        attachment_retrieved = request.context.get(
            "retrieved_context",
            request.context.get("retrieved"),
        )
        if attachment_retrieved:
            agent_metadata["retrieved_context"] = attachment_retrieved
        enriched = request.model_copy(
            update={
                "context": {
                    **request.context,
                    **agent_metadata,
                },
            }
        )
        return await self.agent_runner.run(enriched)


def _resolve_reference_context(
    request: UserRequest,
    business_context: object,
) -> dict[str, str] | None:
    if not any(
        marker in request.text
        for marker in ("这句话", "这段话", "上句话", "上一句话", "刚才那句话")
    ):
        return None
    messages = getattr(business_context, "recent_messages", ())
    for message in reversed(messages):
        content = str(getattr(message, "content", "") or "").strip()
        role = str(getattr(message, "role", "") or "")
        if not content or content == request.text.strip():
            continue
        if content.startswith(
            ("还需要：", "处理失败", "请求意图不够明确", "处理完成。")
        ):
            continue
        if _is_reference_command(content):
            continue
        return {"role": role, "content": content[:4000]}
    return None


def _is_reference_command(content: str) -> bool:
    return any(marker in content for marker in ("这句话", "这段话", "上句话")) and any(
        marker in content for marker in ("写入", "追加", "记录到", "记到")
    )


def _resolve_document_context(
    request: UserRequest,
    reference_context: dict[str, str] | None,
) -> dict[str, str | None] | None:
    normalized = re.sub(r"\s+", "", request.text)
    if not ("文档" in normalized and any(
        marker in normalized for marker in ("写入", "追加", "添加", "记录到", "记到")
    )):
        return None
    if reference_context is None:
        return {
            "action": "append" if "追加" in normalized else "create",
            "title": None,
            "target_title": None,
            "markdown": None,
        }
    content = reference_context.get("content") or ""
    title_match = re.search(r"(?:标题|名字|命名为)[:：]?\s*([^\s，。,]+)", normalized)
    return {
        "action": "append" if "追加" in normalized else "create",
        "title": title_match.group(1) if title_match else None,
        "target_title": content.strip()[:200] if "追加" in normalized else None,
        "markdown": content.strip()[:4000] or None,
    }
