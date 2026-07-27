"""消息与 UI 请求统一路由。"""

from __future__ import annotations

import re

from ..context.builders import AgentRuntimeContextBuilder, BusinessContextBuilder
from ..context.service import ConversationContextService
from ..domain.commands import DirectPendingActionCommand
from ..domain.intents import DETERMINISTIC_INTENTS, UserRequest
from ..domain.results import ApplicationResult
from ..intelligence.general_agent import GeneralAgent
from ..intelligence.intent_extractor import IntentExtractor
from ..llm import LLMTimeoutError
from .calendar_service import CalendarApplicationService
from .pending_action_service import PendingActionApplicationService
from .reminder_service import ReminderApplicationService
from .task_service import TaskApplicationService


class RequestRouter:
    def __init__(
        self,
        intent_extractor: IntentExtractor,
        calendar: CalendarApplicationService,
        tasks: TaskApplicationService,
        reminders: ReminderApplicationService,
        pending_actions: PendingActionApplicationService,
        general_agent: GeneralAgent,
        business_contexts: BusinessContextBuilder,
        conversation_contexts: ConversationContextService,
        agent_contexts: AgentRuntimeContextBuilder,
        *,
        minimum_confidence: float = 0.65,
    ) -> None:
        self.intent_extractor = intent_extractor
        self.calendar = calendar
        self.tasks = tasks
        self.reminders = reminders
        self.pending_actions = pending_actions
        self.general_agent = general_agent
        self.business_contexts = business_contexts
        self.conversation_contexts = conversation_contexts
        self.agent_contexts = agent_contexts
        self.minimum_confidence = minimum_confidence

    async def handle(
        self,
        request: UserRequest,
        direct_action: DirectPendingActionCommand | None = None,
    ) -> ApplicationResult:
        await self.conversation_contexts.record_user(request)
        if direct_action:
            result = await self._dispatch_pending(request, direct_action)
            await self.conversation_contexts.record_assistant(request, result.message)
            return result
        business_context = await self.business_contexts.build_for_intent_extraction(
            request
        )
        try:
            extracted = await self.intent_extractor.extract(request, business_context)
        except LLMTimeoutError:
            result = ApplicationResult(
                status="error",
                intent="conversation.general",
                message="模型服务响应超时，请稍后重试；无需重新授权。",
            )
            await self.conversation_contexts.record_assistant(request, result.message)
            return result
        if extracted.confidence < self.minimum_confidence:
            result = ApplicationResult(
                status="clarification_required",
                intent=extracted.intent,
                message="请求意图不够明确，请补充要操作的对象和时间。",
            )
            await self.conversation_contexts.record_assistant(request, result.message)
            return result
        if extracted.missing_fields:
            result = ApplicationResult(
                status="clarification_required",
                intent=extracted.intent,
                message="还需要：" + "、".join(extracted.missing_fields),
                data={"missing_fields": extracted.missing_fields},
            )
            await self.conversation_contexts.record_assistant(request, result.message)
            return result
        if extracted.intent == "calendar.create":
            result = await self.calendar.prepare_create(
                request,
                extracted.arguments,
            )
        elif extracted.intent == "calendar.query":
            result = await self.calendar.query(request, extracted.arguments)
        elif extracted.intent == "calendar.update":
            result = await self.calendar.prepare_update(request, extracted.arguments)
        elif extracted.intent == "calendar.delete":
            result = await self.calendar.prepare_delete(request, extracted.arguments)
        elif extracted.intent == "task.create":
            result = await self.tasks.prepare_create(request, extracted.arguments)
        elif extracted.intent == "task.query":
            result = await self.tasks.query(request, extracted.arguments)
        elif extracted.intent == "task.update":
            result = await self.tasks.prepare_update(request, extracted.arguments)
        elif extracted.intent == "task.complete":
            result = await self.tasks.prepare_complete(request, extracted.arguments)
        elif extracted.intent == "task.delete":
            result = await self.tasks.prepare_delete(request, extracted.arguments)
        elif extracted.intent == "reminder.create":
            result = await self.reminders.prepare_create(request, extracted.arguments)
        elif extracted.intent == "reminder.batch_create":
            result = await self.reminders.prepare_batch_create(
                request,
                extracted.arguments,
            )
        elif extracted.intent == "reminder.query":
            result = await self.reminders.query(request, extracted.arguments)
        elif extracted.intent == "reminder.cancel":
            result = await self.reminders.prepare_cancel(request, extracted.arguments)
        elif extracted.intent in DETERMINISTIC_INTENTS:
            result = ApplicationResult(
                status="unsupported",
                intent=extracted.intent,
                message=f"{extracted.intent} 已进入确定性路由，但该业务服务尚未启用。",
            )
        else:
            agent_metadata = self.agent_contexts.metadata(business_context)
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
            result = await self.general_agent.run(enriched)
        await self.conversation_contexts.record_assistant(request, result.message)
        return result

    async def _dispatch_pending(
        self,
        request: UserRequest,
        command: DirectPendingActionCommand,
    ) -> ApplicationResult:
        if command.action == "pending_action.confirm":
            return await self.pending_actions.confirm(request.identity, command.action_id)
        if command.action == "pending_action.cancel":
            return await self.pending_actions.cancel(request.identity, command.action_id)
        return await self.pending_actions.retry(request.identity, command.action_id)


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
    title_match = re.search(
        r"(?:中的|里的)([^，。]{1,100}?)(?:云文档|文档)",
        normalized,
    )
    if title_match is None:
        title_match = re.search(
            r"(?:文档叫做|文档名为|名为|叫做)([^，。]{1,100})$",
            normalized,
        )
    target_title = title_match.group(1).strip() if title_match else ""
    explicit_match = re.search(
        r"把(.+?)(?:这句话|这段话)?(?:写入|追加|添加到|记录到|记到)",
        normalized,
    )
    explicit = explicit_match.group(1).strip() if explicit_match else ""
    if explicit in {"这句话", "这段话", "上句话", "上一句话"}:
        explicit = ""
    markdown = explicit or (
        reference_context.get("content", "") if reference_context else ""
    )
    if not target_title or not markdown:
        return None
    return {
        "action": "append",
        "title": None,
        "target_title": target_title,
        "markdown": markdown,
    }
