"""消息与 UI 请求统一路由。"""

from __future__ import annotations

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
            enriched = request.model_copy(
                update={
                    "context": self.agent_contexts.metadata(business_context),
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
