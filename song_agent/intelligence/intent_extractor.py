"""一次性意图分类和字段提取。"""

from __future__ import annotations

import logging

from pydantic import ValidationError

from ..context.models import BusinessContext
from ..domain.intents import ExtractedIntent, UserRequest
from ..llm import LLMInvalidResponseError, StructuredLlm
from .time_parser import current_time_context

SYSTEM_PROMPT = """你是 Song Agent 的意图提取器。
只做意图分类和字段提取，不决定确认、权限、执行、工具或卡片。
输出键必须是 intent、arguments、missing_fields、confidence；禁止使用 intent_type。
calendar.create/reminder.create 的 arguments 使用 CalendarCreateCommand 字段：
summary、start_time、end_time、timezone、description、location、
reminder_minutes、attendee_open_ids、is_all_day、recurrence。
reminder_minutes 必须是整数 JSON 数组，例如立即提醒写 [0]，提前十分钟写 [10]。
reminder.create 只提取触发时间为 start_time，不要输出 end_time。
calendar.query 使用 query、start_time、end_time、event_id、page_size。
calendar.update 使用 event_id 和待修改字段；calendar.delete 使用 event_id。
重复日程更新/删除必须提取 recurrence_scope：single、all 或 future；
用户未说明时将 recurrence_scope 写入 missing_fields。
task.create 使用 summary、description、start_time、due_time、is_all_day、
assignee_open_ids、follower_open_ids、reminder_minutes、repeat_rule、tasklist_guid。
task.query 使用 query、task_guid、completed、page_size。
task.update 使用 task_guid、fields；task.complete/task.delete 使用 task_guid。
reminder.query 使用日历查询字段；reminder.cancel 使用 event_id。
时间必须输出带时区 ISO 8601。相对时间按提供的当前时间计算。
缺少创建所需字段时写入 missing_fields，禁止猜测。
普通对话和开放分析使用 conversation.general/content.summarize/content.analyze。
文档创建、文档追加、计划处理、联网搜索统一使用 conversation.general；
禁止输出 document.create、document.append 等未定义意图。
输出 ExtractedIntent JSON 对象。"""


class IntentExtractor:
    def __init__(self, llm: StructuredLlm, timezone: str) -> None:
        self.llm = llm
        self.timezone = timezone
        self.logger = logging.getLogger(__name__)

    async def extract(
        self,
        request: UserRequest,
        business_context: BusinessContext | None = None,
    ) -> ExtractedIntent:
        context = (
            f"当前时间：{current_time_context(self.timezone)}\n"
            f"默认时区：{self.timezone}\n"
            f"用户请求：{request.text}"
        )
        if business_context:
            recent = "\n".join(
                f"{message.role}: {message.content}"
                for message in business_context.recent_messages[-8:]
            )
            memories = "\n".join(
                f"{memory.memory_key}={memory.memory_value}"
                for memory in business_context.memories
            )
            summary = (
                business_context.conversation_summary.model_dump_json()
                if business_context.conversation_summary
                else "{}"
            )
            context += (
                f"\n最近对话：\n{recent or '无'}"
                f"\n历史摘要：{summary}"
                f"\n相关长期记忆：\n{memories or '无'}"
                f"\n当前待处理业务对象：{business_context.active_pending_action or {}}"
            )
        try:
            return await self.llm.generate(
                ExtractedIntent,
                SYSTEM_PROMPT,
                context,
                run_id=request.message_id or "intent",
                max_tokens=900,
            )
        except (LLMInvalidResponseError, ValidationError) as error:
            repair = (
                f"{context}\n"
                f"上次输出不符合 ExtractedIntent：{str(error)[:500]}\n"
                "修复字段语义，重新输出一个 JSON 对象。"
            )
            try:
                return await self.llm.generate(
                    ExtractedIntent,
                    SYSTEM_PROMPT,
                    repair,
                    run_id=request.message_id or "intent",
                    step_index=1,
                    max_tokens=900,
                )
            except (LLMInvalidResponseError, ValidationError) as repair_error:
                self.logger.warning(
                    "意图提取连续返回无效结构，降级到通用 Agent "
                    "message_id=%s first_error=%s repair_error=%s",
                    request.message_id,
                    str(error)[:200],
                    str(repair_error)[:200],
                )
                return ExtractedIntent(
                    intent="conversation.general",
                    confidence=1.0,
                )
