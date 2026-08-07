"""一次性意图分类和字段提取。"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from ..context.models import BusinessContext
from ..domain.intents import ExtractedIntent, UserRequest
from ..llm import LLMInvalidResponseError, StructuredLlm
from ..observability.context import current_trace_id
from .time_parser import current_time_context

SYSTEM_PROMPT = """你是 Song Agent 的意图提取器。
只做意图分类和字段提取，不决定确认、权限、执行、工具或卡片。
输出键必须是 intent、arguments、missing_fields、confidence；禁止使用 intent_type。
calendar.create/reminder.create 的 arguments 使用 CalendarCreateCommand 字段：
summary、start_time、end_time、timezone、description、location、
reminder_minutes、attendee_open_ids、is_all_day、recurrence。
calendar.create 只要求 summary 和 start_time；end_time 可省略，系统默认持续 60 分钟。
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
一次请求包含多个提醒时使用 reminder.batch_create，arguments 格式为
{"items":[CalendarCreateCommand对象,...]}。
reminder.batch_create 每项只要求 summary 和 start_time；不要把 end_time 写入 missing_fields。
时间必须输出带时区 ISO 8601。相对时间按提供的当前时间计算。
缺少创建所需字段时写入 missing_fields，禁止猜测。
普通对话和开放分析使用 conversation.general/content.summarize/content.analyze。
文档创建、文档追加、计划处理、联网搜索统一使用 conversation.general。
用户说“规划”“制定计划”时，即使列出具体时间，也属于计划处理；
仅当用户明确要求创建日程、加入日历、设置提醒或创建任务时才选择写操作意图。
禁止输出 document.create、document.append 等未定义意图。
输出 ExtractedIntent JSON 对象。"""

_PLANNING_MARKERS = (
    "规划",
    "制定计划",
    "安排计划",
    "计划一下",
    "帮我计划",
    "给我计划",
)
_EXPLICIT_WRITE_MARKERS = (
    "创建日程",
    "新建日程",
    "添加日程",
    "加入日历",
    "加到日历",
    "写入日历",
    "记到日历",
    "放到日历",
    "提醒我",
    "设置提醒",
    "设个提醒",
    "定个提醒",
    "闹钟",
    "创建任务",
    "新建任务",
    "添加任务",
)


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
        started_at = time.monotonic()
        result = await self._extract(request, business_context)
        self.logger.info(
            "perf intent_extraction trace_id=%s duration_ms=%d "
            "intent=%s confidence=%s message_id=%s",
            current_trace_id(),
            int((time.monotonic() - started_at) * 1000),
            result.intent,
            result.confidence,
            request.message_id,
        )
        return result

    async def _extract(
        self,
        request: UserRequest,
        business_context: BusinessContext | None = None,
    ) -> ExtractedIntent:
        batch = _extract_numbered_reminders(request.text, self.timezone)
        if batch is not None:
            return batch
        if _is_planning_request(request.text):
            return ExtractedIntent(
                intent="conversation.general",
                confidence=1.0,
            )
        if _is_document_request(request.text):
            return ExtractedIntent(
                intent="conversation.general",
                confidence=1.0,
            )
        context = (
            f"当前时间：{current_time_context(self.timezone)}\n"
            f"默认时区：{self.timezone}\n"
            f"用户请求：{request.text}"
        )
        attachment_retrieved = request.context.get(
            "retrieved_context",
            request.context.get("retrieved"),
        )
        if attachment_retrieved:
            serialized = json.dumps(
                attachment_retrieved,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            context += f"\n本次附件解析结果：{serialized[:6000]}"
        is_audio_request = "audio" in request.context.get("attachment_kinds", [])
        if business_context and not is_audio_request:
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
            extracted = await self.llm.generate(
                ExtractedIntent,
                SYSTEM_PROMPT,
                context,
                run_id=request.message_id or "intent",
                max_tokens=1800,
            )
            return _normalize_creation_missing_fields(extracted)
        except (LLMInvalidResponseError, ValidationError) as error:
            repair = (
                f"{context}\n"
                f"上次输出不符合 ExtractedIntent：{str(error)[:500]}\n"
                "修复字段语义，重新输出一个 JSON 对象。"
            )
            try:
                extracted = await self.llm.generate(
                    ExtractedIntent,
                    SYSTEM_PROMPT,
                    repair,
                    run_id=request.message_id or "intent",
                    step_index=1,
                    max_tokens=1800,
                )
                return _normalize_creation_missing_fields(extracted)
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


def _is_planning_request(text: str) -> bool:
    normalized = re.sub(r"\s+", "", text)
    return any(marker in normalized for marker in _PLANNING_MARKERS) and not any(
        marker in normalized for marker in _EXPLICIT_WRITE_MARKERS
    )


def _is_document_request(text: str) -> bool:
    normalized = re.sub(r"\s+", "", text)
    has_document = "文档" in normalized or "云文档" in normalized
    has_action = any(
        marker in normalized
        for marker in (
            "写入",
            "追加",
            "添加",
            "记录到",
            "记到",
            "创建",
            "新建",
            "生成",
        )
    )
    return has_document and has_action


def _normalize_creation_missing_fields(
    extracted: ExtractedIntent,
) -> ExtractedIntent:
    arguments = extracted.arguments
    missing_fields: list[str]
    if extracted.intent in {"calendar.create", "reminder.create"}:
        missing_fields = []
        if not _nonempty_text(arguments.get("summary")):
            missing_fields.append("summary")
        if not arguments.get("start_time"):
            missing_fields.append("start_time")
    elif extracted.intent == "task.create":
        missing_fields = (
            [] if _nonempty_text(arguments.get("summary")) else ["summary"]
        )
    elif extracted.intent == "reminder.batch_create":
        items = arguments.get("items")
        if not isinstance(items, list) or len(items) < 2:
            missing_fields = ["items"]
        else:
            missing_fields = []
            for index, item in enumerate(items):
                if not isinstance(item, dict) or not _nonempty_text(item.get("summary")):
                    missing_fields.append(f"items[{index}].summary")
                if not isinstance(item, dict) or not item.get("start_time"):
                    missing_fields.append(f"items[{index}].start_time")
    elif extracted.intent in {
        "calendar.update",
        "calendar.delete",
        "task.update",
        "task.complete",
        "task.delete",
        "reminder.cancel",
    }:
        return extracted
    else:
        return extracted.model_copy(update={"missing_fields": []})
    return extracted.model_copy(update={"missing_fields": missing_fields})


def _nonempty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _extract_numbered_reminders(
    text: str,
    timezone: str,
    *,
    now: datetime | None = None,
) -> ExtractedIntent | None:
    if not any(keyword in text for keyword in ("闹钟", "提醒")):
        return None
    parts = re.findall(
        r"(?:^|\n)\s*\d+[.、)]\s*(.+?)(?=(?:\n\s*\d+[.、)])|\Z)",
        text,
        flags=re.DOTALL,
    )
    if len(parts) < 2:
        return None
    zone = ZoneInfo(timezone)
    current = now.astimezone(zone) if now else datetime.now(zone)
    items: list[dict] = []
    for part in parts:
        match = re.search(
            r"(?:(早上|上午|中午|下午|晚上|夜里)\s*)?"
            r"(\d{1,2})点(?:钟)?(?:(半)|(\d{1,2})分)?",
            part,
        )
        if match is None:
            return None
        period, hour_text, half, minute_text = match.groups()
        hour = int(hour_text)
        minute = 30 if half else int(minute_text or 0)
        if period in {"下午", "晚上", "夜里"} and hour < 12:
            hour += 12
        elif period == "中午" and hour < 11:
            hour += 12
        if hour > 23 or minute > 59:
            return None
        recurrence = "FREQ=DAILY" if "每天" in part else None
        day_offset = 2 if "后天" in part else 1 if "明天" in part else 0
        start = datetime.combine(
            current.date() + timedelta(days=day_offset),
            datetime.min.time(),
            tzinfo=zone,
        ).replace(hour=hour, minute=minute)
        if recurrence and start <= current:
            start += timedelta(days=1)
        if not recurrence and start <= current and day_offset == 0:
            return None
        summary = part[match.end():]
        summary = re.sub(r"(?:的)?(?:闹钟|提醒)\s*$", "", summary).strip()
        items.append(
            {
                "summary": summary or "提醒",
                "start_time": start.isoformat(),
                "timezone": timezone,
                "reminder_minutes": [0],
                "recurrence": recurrence,
            }
        )
    return ExtractedIntent(
        intent="reminder.batch_create",
        arguments={"items": items},
        confidence=1.0,
    )
