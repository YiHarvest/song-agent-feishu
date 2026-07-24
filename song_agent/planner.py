"""
计划解析模块。

使用 LLM 解析用户输入，生成计划、复盘、文档等内容。
支持基于规则的意图识别和启发式检测。
"""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from .llm import StructuredLlm
from .models import (
    ActionIntentOutput,
    DailyRecord,
    ParsedPlanTask,
    PlanTask,
    ReviewUpdate,
)

Intent = str


class Planner:
    """
    计划解析器。

    使用 LLM 解析用户输入，生成计划、复盘、文档等结构化内容。
    支持基于规则的意图识别和启发式检测。
    """

    def __init__(self, llm: StructuredLlm, timezone: str) -> None:
        self.llm = llm
        self.timezone = timezone

    def today(self) -> str:
        return datetime.now(ZoneInfo(self.timezone)).date().isoformat()

def detect_exact_intent(text: str) -> ActionIntentOutput | None:
    """Route only explicit commands and exact conversational phrases."""
    normalized = text.strip().lower()
    if re.fullmatch(
        r"(你好|您好|嗨|哈喽|hello|hi|在吗|谢谢|感谢|你是谁|"
        r"你能做什么|你会做什么|有什么功能|能帮我做什么)[？?！!。.～~\s]*",
        normalized,
    ):
        return ActionIntentOutput(
            intent="chat.reply",
            confidence=1,
            requires_confirmation=False,
        )
    command_match = re.match(r"^/(plan|计划|remind|提醒|review|复盘|doc|文档)\b", normalized)
    if not command_match:
        return None
    command = command_match.group(1)
    mapping = {
        "plan": "plan.create",
        "计划": "plan.create",
        "remind": "calendar.create",
        "提醒": "calendar.create",
        "review": "review.create",
        "复盘": "review.create",
        "doc": "document.create",
        "文档": "document.create",
    }
    intent = mapping[command]
    return ActionIntentOutput(
        intent=intent,
        confidence=1,
        requires_confirmation=True,
    )


def detect_intent_heuristically(text: str, record: DailyRecord | None = None) -> Intent:
    """Backward-compatible helper; arbitrary natural language always returns unknown."""
    del record
    exact = detect_exact_intent(text)
    if not exact:
        return "unknown"
    return {
        "plan.create": "plan",
        "calendar.create": "reminder",
        "review.create": "review",
        "document.create": "document",
        "document.append": "document",
        "chat.reply": "chat",
        "unknown": "unknown",
    }[exact.intent]


def is_confirmation(text: str) -> bool:
    return bool(re.fullmatch(r"(确认|确认计划|创建日程|确认并创建日程)[。！!\s]*", text.strip()))


def is_clear_command(text: str) -> bool:
    """接受文档化的斜杠命令和自然语言中文命令。"""
    return bool(re.fullmatch(r"/?(?:clear|清空)(?:计划)?[。！!\s]*", text.strip(), re.IGNORECASE))


def has_explicit_time_hint(text: str) -> bool:
    return bool(
        re.search(
            r"(?:\d+|[零〇一二两三四五六七八九十百]+)\s*分钟后|"
            r"(?:今天|今日|明天|后天|上午|中午|下午|晚上|凌晨)|"
            r"(?:[01]?\d|2[0-3])\s*[:：]\s*[0-5]\d|"
            r"(?:[零〇一二两三四五六七八九十百]+|\d{1,2})\s*点",
            text,
        )
    )


def is_reminder_status_question(text: str) -> bool:
    normalized = re.sub(r"\s+", "", text.strip().lower())
    return bool(
        re.search(
            r"(?:闹钟|提醒|日程).{0,12}(?:定|设|创建|加).{0,3}(?:了吗|了没|好了吗|成功了吗)|"
            r"(?:定|设|创建|加).{0,8}(?:闹钟|提醒|日程).{0,3}(?:了吗|了没|好了吗|成功了吗)",
            normalized,
        )
    )


def build_tasks(items: list[ParsedPlanTask]) -> list[PlanTask]:
    counters = {"A": 0, "B": 0, "C": 0}
    result: list[PlanTask] = []
    for item in items:
        counters[item.priority] += 1
        result.append(
            PlanTask(
                id=f"{item.priority}{counters[item.priority]}",
                priority=item.priority,
                title=item.title,
                start_time=item.start_time,
                end_time=item.end_time,
                repeat=item.repeat,
            )
        )
    return result


def apply_review(record: DailyRecord, updates: list[ReviewUpdate]) -> int:
    by_id = {item.task_id: item for item in updates}
    points = 0.0
    for task in record.tasks:
        update = by_id.get(task.id)
        if not update:
            task.status, task.completion_ratio = "unconfirmed", None
        else:
            task.status = update.status
            task.completion_ratio = update.completion_ratio if update.status == "partial" else None
        if task.status == "completed":
            points += 1
        elif task.status == "partial":
            points += (task.completion_ratio or 0) / 100
    return round(points / len(record.tasks) * 100) if record.tasks else 0


def format_plan(record: DailyRecord) -> str:
    labels = (("A", "🔴 A类（关键必做）"), ("B", "🟡 B类（重要）"), ("C", "🟢 C类（辅助）"))
    repeat_labels = {"daily": "📅 每天", "weekdays": "📅 工作日", "weekly": "📅 每周", "none": ""}
    lines = [f"#### 📋 {record.date} 日计划", ""]
    for priority, label in labels:
        tasks = [item for item in record.tasks if item.priority == priority]
        if not tasks:
            continue
        lines.append(f"**{label}**")
        for task in tasks:
            timing = "灵活安排"
            if task.start_time:
                timing = f"{task.start_time}-{task.end_time}" if task.end_time else task.start_time
            repeat_str = repeat_labels.get(task.repeat, "")
            if repeat_str:
                timing = f"{timing}｜{repeat_str}"
            lines.append(f"- **{task.id}** {task.title}｜{timing}")
        lines.append("")
    if record.plan_status == "draft" and any(task.start_time for task in record.tasks):
        lines.append("请点击确认卡片后，我才会写入你自己的飞书日历。")
    return "\n".join(lines)


def format_review(record: DailyRecord) -> str:
    icons = {
        "pending": "⏳ 待执行",
        "completed": "✅ 已完成",
        "partial": "🔶 部分完成",
        "not_done": "❌ 未完成",
        "unconfirmed": "❓ 未确认",
    }
    lines = [
        f"#### 📝 {record.date} 复盘",
        "",
        f"**完成率：{record.review.completion_rate if record.review else 0}%**",
        "",
    ]
    for task in record.tasks:
        ratio = f"（{task.completion_ratio or 0:g}%）" if task.status == "partial" else ""
        lines.append(f"- **{task.id}** {task.title} → {icons[task.status]}{ratio}")
    if record.review:
        lines.extend(["", f"**今日总结：** {record.review.summary}"])
        if record.review.insights:
            lines.extend(["", "**今日心得：**", *[f"- {item}" for item in record.review.insights]])
    return "\n".join(lines)
