"""
Agent 工作流模块。

处理消息分发、意图识别、计划创建、文档生成等核心业务逻辑。
支持自然语言交互，自动识别用户意图。
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime

from .feishu.mcp import FeishuMcp
from .feishu.oauth import FeishuOAuth
from .feishu.transport import FeishuTransport, clean_incoming_text
from .models import DailyRecord, DailyReview, DocumentBinding, IncomingMessage
from .planner import (
    Planner,
    apply_review,
    build_tasks,
    format_plan,
    format_review,
    has_explicit_time_hint,
    is_clear_command,
    is_confirmation,
    is_reminder_status_question,
)
from .store import JsonStore


class AgentWorkflow:
    """
    Agent 工作流处理器。

    处理消息分发、意图识别、计划创建、文档生成等核心业务逻辑。
    支持自然语言交互，自动识别用户意图。
    """

    def __init__(
        self,
        planner: Planner,
        store: JsonStore,
        transport: FeishuTransport,
        oauth: FeishuOAuth,
        mcp: FeishuMcp,
    ) -> None:
        self.planner = planner
        self.store = store
        self.transport = transport
        self.oauth = oauth
        self.mcp = mcp
        self.logger = logging.getLogger(__name__)
        self._locks: dict[str, asyncio.Lock] = {}

    async def enqueue(self, message: IncomingMessage) -> None:
        key = f"{message.chat_id}:{message.user_id}"
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            try:
                await self._handle(message)
            except Exception:
                self.logger.exception("Agent 工作流执行失败")
                await self.transport.send_markdown(
                    message.chat_id,
                    "处理失败，详细原因已写入服务日志。请稍后重试；若刚调整过权限，请重新完成你自己的飞书授权。",
                )
        if not lock.locked():
            self._locks.pop(key, None)

    async def _handle(self, message: IncomingMessage) -> None:
        if self.store.has_processed_message(message.message_id):
            return
        await self.store.mark_processed(message.message_id)
        if message.message_type not in {"text", "post"}:
            await self.transport.send_markdown(message.chat_id, "当前版本只接收文字消息。")
            return
        text = clean_incoming_text(message.text)
        if not text:
            return
        self.logger.info(
            "📩 收到用户消息 message_id=%s chat=%s user=%s type=%s text=%r",
            message.message_id,
            message.chat_id,
            message.user_id,
            message.chat_type,
            text,
        )
        date = self.planner.today()
        record = self.store.get_record(message.chat_id, message.user_id, date)

        if re.match(r"^/(help|帮助)\b", text, re.IGNORECASE):
            await self.transport.send_markdown(message.chat_id, help_message())
        elif re.match(r"^/(status|状态)\b", text, re.IGNORECASE):
            await self.transport.send_markdown(
                message.chat_id, format_plan(record) if record else f"你在今天（{date}）还没有计划。"
            )
        elif is_clear_command(text):
            await self.store.delete_record(message.chat_id, message.user_id, date)
            await self.transport.send_markdown(
                message.chat_id, "已清空你今天的本地计划；不会影响其他群成员。"
            )
        elif is_reminder_status_question(text):
            await self._reminder_status(message, record)
        elif is_confirmation(text):
            await self._confirm_plan(message, record)
        else:
            intent = await self.planner.detect_intent(text, record)
            self.logger.info("🧭 管家处理决策 message_id=%s intent=%s", message.message_id, intent)
            if intent == "plan":
                await self._create_draft(message, record, text, date)
            elif intent == "reminder":
                await self._create_draft(message, record, text, date, auto_confirm=True)
            elif intent == "review":
                await self._create_review(message, record, text)
            elif intent == "document":
                await self._create_document(message, text)
            elif intent == "chat":
                await self.transport.send_markdown(message.chat_id, await self.planner.chat(text))
            else:
                await self.transport.send_markdown(
                    message.chat_id,
                    "我不太确定你希望我执行什么操作。你可以换一种说法，或发送 `/help` 查看我目前支持的功能。",
                )

    async def _create_draft(
        self,
        message: IncomingMessage,
        existing: DailyRecord | None,
        text: str,
        date: str,
        *,
        auto_confirm: bool = False,
    ) -> None:
        has_time_hint = has_explicit_time_hint(text)
        self.logger.info(
            "🧠 计划处理：解析任务 has_existing=%s has_time_hint=%s auto_confirm=%s",
            bool(existing),
            has_time_hint,
            auto_confirm,
        )

        if existing and existing.plan_status == "confirmed" and not has_time_hint:
            await self.transport.send_markdown(
                message.chat_id,
                "你今天的计划已经确认。若需重做，请先发送 `/clear` 或 `清空`，"
                "并自行检查你日历中的旧日程。\n\n"
                '如果是想添加**带时间的提醒**，请说"X分钟后提醒我..."或"X点提醒我..."。',
            )
            return

        await self.transport.send_markdown(message.chat_id, "正在整理你的计划或提醒...")
        tasks = await self.planner.parse_plan(text, date)
        if not tasks:
            await self.transport.send_markdown(
                message.chat_id,
                "可以，请把今天要做的具体事项告诉我，例如：吃饭、购物、提交材料。有明确时间也可以一起说。",
            )
            return
        now = datetime.now(UTC).isoformat()

        new_task_ids: set[str]
        if existing and has_time_hint:
            # 追加新的带时间的任务到现有计划
            existing_tasks = list(existing.tasks)
            new_tasks = build_tasks(tasks)
            # 重新编号新任务
            counters = {"A": 0, "B": 0, "C": 0}
            for t in existing_tasks:
                counters[t.priority] = counters.get(t.priority, 0) + 1
            for t in new_tasks:
                counters[t.priority] = counters.get(t.priority, 0) + 1
                t.id = f"{t.priority}{counters[t.priority]}"
            new_task_ids = {task.id for task in new_tasks}
            all_tasks = existing_tasks + new_tasks
            record = DailyRecord(
                key=self.store.record_key(message.chat_id, message.user_id, date),
                date=date,
                chat_id=message.chat_id,
                user_id=message.user_id,
                # 新增的带时间事项必须重新确认，不能沿用旧计划的 confirmed 状态。
                plan_status="draft",
                tasks=all_tasks,
                created_at=existing.created_at,
                updated_at=now,
            )
        else:
            record = DailyRecord(
                key=self.store.record_key(message.chat_id, message.user_id, date),
                date=date,
                chat_id=message.chat_id,
                user_id=message.user_id,
                plan_status="draft",
                tasks=build_tasks(tasks),
                created_at=existing.created_at if existing else now,
                updated_at=now,
            )
            new_task_ids = {task.id for task in record.tasks}
        await self.store.save_record(record)
        if auto_confirm:
            if any(
                task.id in new_task_ids and task.start_time and not task.calendar_event_id
                for task in record.tasks
            ):
                await self._confirm_plan(message, record, task_ids=new_task_ids, concise=True)
            else:
                await self.transport.send_markdown(
                    message.chat_id,
                    "我识别到你要设置提醒，但没有得到明确可执行的时间。请补充例如“10 分钟后”或“15:30”。",
                )
        else:
            await self.transport.send_markdown(message.chat_id, format_plan(record))

    async def _reminder_status(self, message: IncomingMessage, record: DailyRecord | None) -> None:
        timed = [task for task in record.tasks if task.start_time] if record else []
        if not timed:
            await self.transport.send_markdown(message.chat_id, "今天还没有带时间的提醒或日程。")
            return
        created = [task for task in timed if task.calendar_event_id]
        pending = [task for task in timed if not task.calendar_event_id]
        lines: list[str] = []
        if created:
            lines.append(
                "✅ 已创建到你本人日历："
                + "、".join(f"{task.title}（{task.start_time}）" for task in created)
            )
        if pending:
            lines.append(
                "⏳ 尚未创建："
                + "、".join(f"{task.title}（{task.start_time}）" for task in pending)
                + "。请回复 **确认** 后，我才会实际写入你本人的飞书日历。"
            )
        await self.transport.send_markdown(message.chat_id, "\n\n".join(lines))

    async def _confirm_plan(
        self,
        message: IncomingMessage,
        record: DailyRecord | None,
        *,
        task_ids: set[str] | None = None,
        concise: bool = False,
    ) -> None:
        self.logger.info("🧠 日历处理：检查计划、用户授权和待创建日程")
        if not record:
            await self.transport.send_markdown(
                message.chat_id, "你今天还没有待确认的计划，请先告诉我需要什么提醒。"
            )
            return
        if record.plan_status == "confirmed":
            # 检查是否有新添加的带时间的任务需要写入日历
            new_events = [t for t in record.tasks if t.start_time and not t.calendar_event_id]
            if new_events:
                # 有新任务需要写入
                pass
            else:
                await self.transport.send_markdown(message.chat_id, "你的计划已经确认，不会重复创建日程。")
                return
        if record.user_id != message.user_id:
            await self.transport.send_markdown(message.chat_id, "只能确认你自己创建的计划。")
            return
        access_token = await self.oauth.get_valid_access_token(
            message.user_id, ("calendar:calendar", "calendar:calendar:readonly")
        )
        if not access_token:
            url = self.oauth.create_authorization_url(message.user_id, message.chat_id)
            await self.transport.send_markdown(
                message.chat_id,
                f"创建日程需要你本人授权。[点击这里完成授权]({url})，然后由你再次回复 **确认**。"
                "每位群成员的授权与日历完全隔离。",
            )
            return
        await self.transport.send_markdown(message.chat_id, "正在通过飞书 MCP 写入你本人的日历...")
        result = await self.mcp.create_events(record, access_token, task_ids)
        event_ids = dict(result.created)
        for task in record.tasks:
            if task.id in event_ids:
                task.calendar_event_id = event_ids[task.id]
        if record.plan_status != "confirmed":
            record.plan_status = "draft" if result.failed else "confirmed"
        record.updated_at = datetime.now(UTC).isoformat()
        await self.store.save_record(record)
        if concise:
            created_ids = {item[0] for item in result.created}
            created_tasks = [task for task in record.tasks if task.id in created_ids]
            details = "、".join(f"{task.title}（{task.start_time}）" for task in created_tasks)
            lines = [f"✅ 已在你本人的日历创建提醒：{details}（提前 10 分钟）。"]
        else:
            lines = [
                format_plan(record),
                "",
                f"✅ 已在你本人的日历创建 {len(result.created)} 个提醒（提前 10 分钟）。",
            ]
        if result.skipped:
            lines.append(f"无明确时间、未创建日程：{'、'.join(result.skipped)}")
        if result.failed:
            lines.append(f"创建失败：{'、'.join(item[0] for item in result.failed)}；可再次回复确认重试。")
        await self.transport.send_markdown(message.chat_id, "\n".join(lines))

    async def _create_document(self, message: IncomingMessage, text: str) -> None:
        self.logger.info("🧠 文档处理：解析新建或追加意图")
        draft = await self.planner.parse_document(text)
        explicit_token = extract_docx_token(text)
        required_scopes = (
            ("docx:document", "drive:drive")
            if draft.action == "append" and not explicit_token
            else ("docx:document",)
        )
        access_token = await self.oauth.get_valid_access_token(message.user_id, required_scopes)
        if not access_token:
            url = self.oauth.create_authorization_url(message.user_id, message.chat_id)
            await self.transport.send_markdown(
                message.chat_id,
                f"创建云文档需要你本人授权。[点击这里完成授权]({url})，授权后重新发送文档要求。"
                "新建文档会进入你自己的云空间；追加操作会写入你有权限的目标文档。",
            )
            return
        if draft.action == "append":
            target_title = (draft.target_title or draft.title or "").strip()
            generic_target = target_title in {
                "",
                "我的文档",
                "文档",
                "该文档",
                "这个文档",
                "刚才的文档",
                "上一份文档",
            }
            recent = self.store.get_document_binding(message.chat_id, message.user_id)
            if generic_target and recent and not explicit_token:
                target_title = recent.title
                explicit_token = recent.token
            if not target_title and not explicit_token:
                await self.transport.send_markdown(
                    message.chat_id, "请告诉我要追加的文档标题，或直接附上目标飞书文档链接。"
                )
                return
            if re.search(r"这个|这项|该任务|上述|上面|刚才", text) and "待补充" in draft.markdown:
                await self.transport.send_markdown(
                    message.chat_id,
                    "我识别到你要追加文档，但“这个任务”的具体内容不在当前消息中。"
                    "请把任务内容写在同一条指令里；我不会把“待补充”或猜测内容写进群文档。",
                )
                return
            target_token = explicit_token
            target_url = (
                recent.url
                if recent and target_token == recent.token
                else f"https://feishu.cn/docx/{target_token}"
                if target_token
                else ""
            )
            if not target_token:
                await self.transport.send_markdown(
                    message.chat_id, f"正在该群关联的云文档中查找“{target_title}”..."
                )
                documents = await self.mcp.search_documents(
                    target_title,
                    access_token,
                    chat_id=message.chat_id if message.chat_type == "group" else None,
                )
                exact = [item for item in documents if item.title.strip() == target_title]
                candidates = exact or documents
                if not candidates:
                    await self.transport.send_markdown(
                        message.chat_id,
                        f"没有在该群中找到“{target_title}”。请在消息中附上目标飞书文档链接后重试。",
                    )
                    return
                if len(candidates) > 1:
                    links = "\n".join(f"- [{item.title}]({item.url})" for item in candidates[:10])
                    await self.transport.send_markdown(
                        message.chat_id,
                        f"找到多个可能的目标文档，请把正确文档链接附在指令中：\n{links}",
                    )
                    return
                target_token = candidates[0].token
                target_url = candidates[0].url
            await self.transport.send_markdown(message.chat_id, "正在追加到目标飞书云文档...")
            document = await self.mcp.append_document(
                target_token, target_title, draft.markdown, access_token
            )
            document.url = target_url or document.url
            action_text = "已追加到"
        else:
            await self.transport.send_markdown(message.chat_id, "正在撰写并创建你的飞书云文档...")
            document = await self.mcp.create_document(
                draft.title or "Agent 云文档", draft.markdown, access_token
            )
            action_text = "已创建到你自己的云空间"
        await self.store.save_document_binding(
            DocumentBinding(
                chat_id=message.chat_id,
                user_id=message.user_id,
                title=document.title,
                token=document.token,
                url=document.url,
            )
        )
        safe_title = re.sub(r"[\[\]]", "", document.title)
        await self.transport.send_markdown(
            message.chat_id, f"✅ {action_text}：[{safe_title}]({document.url})"
        )

    async def _create_review(self, message: IncomingMessage, record: DailyRecord | None, text: str) -> None:
        self.logger.info("🧠 复盘处理：检查当日计划并解析完成情况")
        if not record:
            await self.transport.send_markdown(message.chat_id, "找不到你今天的计划，暂时无法复盘。")
            return
        await self.transport.send_markdown(message.chat_id, "正在整理你的复盘...")
        parsed = await self.planner.parse_review(record, text)
        rate = apply_review(record, parsed.updates)
        record.review = DailyReview(
            created_at=datetime.now(UTC).isoformat(),
            completion_rate=rate,
            summary=parsed.summary,
            insights=parsed.insights,
        )
        record.updated_at = datetime.now(UTC).isoformat()
        await self.store.save_record(record)
        await self.transport.send_markdown(message.chat_id, format_review(record))


def extract_docx_token(text: str) -> str | None:
    match = re.search(r"https?://[^\s)]+/docx/([A-Za-z0-9_-]+)", text)
    return match.group(1) if match else None


def help_message() -> str:
    return "\n".join(
        [
            "你好！我是**宋老师管家**，你的 AI 个人管理助手。以下是我能帮你做的事情：",
            "",
            "---",
            "",
            "## 📋 每日规划与复盘",
            "",
            "- **早规划**：帮你整理当天任务，按优先级分为：",
            "  - **A（必须完成）**、**B（重要）**、**C（可选）**",
            "  - 有时间要求的按时间排序，没时间的标注「灵活安排」",
            "- **晚复盘**：回顾当日完成情况，整理未完成原因、经验阻碍，并列出明日计划",
            "- 支持将规划或复盘内容**归档保存**到飞书云文档「每日记录」",
            "",
            "---",
            "",
            "## ⏰ 提醒与日程",
            "",
            "- 创建**单次提醒**或**周期提醒**（每天/工作日/每周）",
            "- 可设置提前提醒时间，到点自动通知你",
            "",
            "---",
            "",
            "## 📄 飞书云文档",
            "",
            "- 查看文档信息与正文内容",
            "- 在文档末尾**追加内容**（不覆盖已有内容）",
            "- 搜索文档",
            "",
            "---",
            "",
            "## ⚙️ 我的原则",
            "",
            "- 只依据你**明确表达**的内容来记录，不猜测、不捏造",
            "- 操作结果如实反馈，成功与否都说清楚",
            "- 涉及提醒、发消息、写文档时，必须实际调用工具，不会口头敷衍",
            "",
            "---",
            "",
            "有什么需要我帮忙的？比如做今天的规划，或者设置一个提醒？",
        ]
    )
