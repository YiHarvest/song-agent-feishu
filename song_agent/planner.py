"""
计划解析模块。

使用 LLM 解析用户输入，生成计划、复盘、文档等内容。
支持基于规则的意图识别和启发式检测。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from .llm import StructuredLlm
from .models import (
    ChatOutput,
    DailyRecord,
    DocumentOutput,
    IntentOutput,
    ParsedPlanTask,
    PlanOutput,
    PlanTask,
    ReviewOutput,
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
        self.logger = logging.getLogger(__name__)

    def today(self) -> str:
        return datetime.now(ZoneInfo(self.timezone)).date().isoformat()

    async def detect_intent(self, text: str, record: DailyRecord | None = None) -> Intent:
        heuristic = detect_intent_heuristically(text, record)
        if heuristic != "unknown":
            self.logger.info("🔀 意图判断：规则命中 intent=%s", heuristic)
            return heuristic
        self.logger.info("🔀 意图判断：规则未命中，交给 LLM 分类")
        result = await self.llm.generate(
            IntentOutput,
            "你是个人管理助手的意图分类器。只输出 JSON。"
            "reminder=用户明确要求设置闹钟、提醒或创建带时间的日程；"
            "plan=整理待办或规划日程但没有明确要求立即设置提醒；"
            "review=反馈完成情况；document=明确要求撰写或创建飞书云文档；"
            "chat=问候、能力咨询、感谢或可以直接回答的普通对话；"
            "只有语义确实无法判断时才用 unknown。",
            f"当前是否已有计划：{'是' if record else '否'}\n用户消息：{text}",
        )
        self.logger.info("🔀 意图判断：LLM 分类 intent=%s", result.intent)
        return result.intent

    async def chat(self, text: str) -> str:
        result = await self.llm.generate(
            ChatOutput,
            "\n".join(
                [
                    "你是“宋老师管家”，一个友好、简洁的中文 AI 个人管理助手。只输出 JSON。",
                    "自然回答问候、感谢和普通问题，不要把它们说成无法理解的指令。",
                    "你的可执行能力仅包括：每日计划与复盘、飞书提醒/日程、飞书云文档创建/追加/搜索。",
                    "不要声称已执行任何工具操作，也不要虚构其他能力。",
                    '输出格式：{"reply":"..."}',
                ]
            ),
            text,
        )
        return result.reply

    async def parse_plan(self, text: str, date: str) -> list[ParsedPlanTask]:
        output = await self.llm.generate(
            PlanOutput,
            "\n".join(
                [
                    "你是严谨的每日计划和提醒整理助手。只输出 JSON。",
                    "把用户明确提到的事项拆成任务，不能虚构任务或时间。",
                    "A=当天关键必须完成；B=重要但可延后；C=辅助或生活平衡。",
                    "明确时间转换成 24 小时 HH:mm；相对时间根据下面提供的当前时间计算。",
                    "没有明确时间必须填 null，不能猜测。",
                    "",
                    "**周期性提醒规则**：",
                    '- 用户说"每天"、"每日" -> repeat="daily"',
                    '- 用户说"工作日"、"周一到周五" -> repeat="weekdays"',
                    '- 用户说"每周"、"每星期" -> repeat="weekly"',
                    '- 无周期关键词 -> repeat="none"',
                    "",
                    f"计划日期为 {date}，时区为 {self.timezone}。",
                    f"当前时间为 {datetime.now(ZoneInfo(self.timezone)).isoformat(timespec='minutes')}。",
                    '{"tasks":[{"title":"...","priority":"A","start_time":"13:00","end_time":null,"repeat":"none"}]}',
                ]
            ),
            text,
        )
        return output.tasks

    async def parse_review(self, record: DailyRecord, text: str) -> ReviewOutput:
        task_list = [{"id": item.id, "title": item.title} for item in record.tasks]
        result = await self.llm.generate(
            ReviewOutput,
            "\n".join(
                [
                    "你是严谨的每日复盘整理助手。只输出 JSON。",
                    "只能根据用户明确反馈判断，禁止猜测。",
                    "completed=明确完成；partial=一部分；not_done=明确没做；unconfirmed=未提及。",
                    "updates 必须包含每个 task_id；partial 才填写 completion_ratio。",
                    f"任务清单：{json.dumps(task_list, ensure_ascii=False)}",
                ]
            ),
            text,
        )
        updates = {item.task_id: item for item in result.updates}
        result.updates = [
            updates.get(task.id, ReviewUpdate(task_id=task.id, status="unconfirmed")) for task in record.tasks
        ]
        return result

    async def parse_document(self, text: str) -> DocumentOutput:
        return await self.llm.generate(
            DocumentOutput,
            "\n".join(
                [
                    "你是专业的中文文档撰写助手。只输出 JSON。",
                    "严格依据用户要求撰写可直接导入飞书云文档的 Markdown。",
                    "用户要求追加、写入已有文档时 action=append，并从原话提取 target_title；"
                    "只有明确要求新建文档时 action=create。",
                    "不得虚构事实、数据、人物或引用；信息不足时标记“待补充”。",
                    "新建时 title 是纯文本标题；追加时 title 可以为 null；markdown 结构清晰。",
                    '{"action":"create|append","target_title":"已有文档标题或null",'
                    '"title":"内容标题","markdown":"# ...\\n\\n..."}',
                ]
            ),
            text,
        )


def detect_intent_heuristically(text: str, record: DailyRecord | None = None) -> Intent:
    normalized = text.strip().lower()
    if re.fullmatch(
        r"(你好|您好|嗨|哈喽|hello|hi|在吗|谢谢|感谢|你是谁|"
        r"你能做什么|你会做什么|有什么功能|能帮我做什么)[？?！!。.～~\s]*",
        normalized,
    ):
        return "chat"
    if re.search(r"^/(doc|文档)\b", normalized) or re.search(
        r"(写|撰写|创建|生成|整理|追加|写入|插入|更新).{0,12}(我的文档|文档|云文档|飞书文档)|"
        r"(我的文档|文档|云文档|飞书文档).{0,12}(写|撰写|创建|生成|整理|追加|写入|插入|更新)",
        normalized,
    ):
        return "document"
    if re.search(
        r"(定|设置|创建|加).{0,30}(闹钟|提醒)|"
        r"(闹钟|提醒).{0,30}(定|设置|创建|加)|"
        r"(?:\d+|[零〇一二两三四五六七八九十百]+)\s*分钟后.{0,30}(提醒|闹钟)|"
        r"(?:提醒我|叫我)",
        normalized,
    ):
        return "reminder"
    if re.search(r"^/(plan|计划)\b", normalized) or re.search(
        r"(今天|今日).*(安排|计划|规划|要做)|工作安排|待办",
        normalized,
    ):
        return "plan"
    if re.search(r"^/(review|复盘)\b|复盘|完成了|已完成|没做|未完成|做了一半|部分完成", normalized):
        return "review"
    if record and record.plan_status == "confirmed" and re.search(r"完成|做完|推进|没弄|没干", normalized):
        return "review"
    return "unknown"


def is_confirmation(text: str) -> bool:
    return bool(re.fullmatch(r"(确认|确认计划|创建日程|确认并创建日程)[。！!\s]*", text.strip()))


def is_clear_command(text: str) -> bool:
    """Accept the documented slash command and the natural Chinese command."""
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
    if record.plan_status == "draft":
        lines.append('回复 "确认" 后，我才会写入你自己的飞书日历。')
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
