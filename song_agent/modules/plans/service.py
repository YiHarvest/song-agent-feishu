"""每日计划模块：本地计划记录查询与操作。

围绕本地 `daily_plans` 记录的确定性能力：
- `/status` 今日状态
- `/clear` 清空今日本地计划
- 提醒状态问答

Channel 只负责调用本模块；本模块不依赖 Transport / Dispatcher / Agent。
"""

from __future__ import annotations

from ...domain.results import ApplicationResult
from ...models import FeishuIdentity
from ...planner import format_plan
from ...store import SqliteStore


class PlanModule:
    def __init__(
        self,
        store: SqliteStore,
        *,
        timezone: str = "Asia/Shanghai",
    ) -> None:
        self.store = store
        self.timezone = timezone

    async def get_today_status(
        self,
        identity: FeishuIdentity,
        chat_id: str,
        thread_id: str,
        *,
        date: str,
    ) -> ApplicationResult:
        record = await self._today_record(identity, chat_id, thread_id, date)
        if not record:
            return ApplicationResult(
                status="ok",
                intent="plans.status",
                message=f"你在今天（{date}）还没有计划。",
            )
        return ApplicationResult(
            status="ok",
            intent="plans.status",
            message=format_plan(record),
        )

    async def clear_today(
        self,
        identity: FeishuIdentity,
        chat_id: str,
        thread_id: str,
        *,
        date: str,
    ) -> ApplicationResult:
        await self.store.delete_record(
            chat_id,
            identity.subject_id,
            date,
            tenant_key=identity.tenant_key,
            app_id=identity.app_id,
            thread_id=thread_id,
        )
        return ApplicationResult(
            status="ok",
            intent="plans.clear",
            message="已清空你今天的本地计划；不会影响其他群成员。",
        )

    async def get_reminder_status(
        self,
        identity: FeishuIdentity,
        chat_id: str,
        thread_id: str,
        *,
        date: str,
    ) -> ApplicationResult:
        record = await self._today_record(identity, chat_id, thread_id, date)
        timed = [task for task in record.tasks if task.start_time] if record else []
        if not timed:
            return ApplicationResult(
                status="ok",
                intent="plans.reminder_status",
                message="今天还没有带时间的提醒或日程。",
            )
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
                + "。请点击最新计划卡片上的 **确认创建**。"
            )
        return ApplicationResult(
            status="ok",
            intent="plans.reminder_status",
            message="\n\n".join(lines),
        )

    async def _today_record(
        self,
        identity: FeishuIdentity,
        chat_id: str,
        thread_id: str,
        date: str,
    ):
        return await self.store.get_record(
            chat_id,
            identity.subject_id,
            date,
            tenant_key=identity.tenant_key,
            app_id=identity.app_id,
            thread_id=thread_id,
        )
