"""提醒作为带 Song Agent 标记的飞书日程管理。"""

from __future__ import annotations

from ..domain.intents import UserRequest
from ..domain.results import ApplicationResult
from .calendar_service import CalendarApplicationService

REMINDER_MARKER = "[song-agent:reminder]"


class ReminderApplicationService:
    def __init__(self, calendar: CalendarApplicationService) -> None:
        self.calendar = calendar

    async def prepare_create(
        self,
        request: UserRequest,
        arguments: dict,
    ) -> ApplicationResult:
        arguments = dict(arguments)
        description = str(arguments.get("description") or "")
        arguments["description"] = f"{REMINDER_MARKER}\n{description}".strip()
        arguments.setdefault("reminder_minutes", [0])
        return await self.calendar.prepare_create(
            request,
            arguments,
            action_type="reminder.create",
        )

    async def query(
        self,
        request: UserRequest,
        arguments: dict,
    ) -> ApplicationResult:
        result = await self.calendar.query(request, arguments)
        if result.status != "ok":
            return result
        items = result.data.get("items")
        if isinstance(items, list):
            result.data["items"] = [
                item
                for item in items
                if REMINDER_MARKER in str(item.get("description") or "")
            ]
        else:
            event = (
                result.data.get("event")
                if isinstance(result.data.get("event"), dict)
                else result.data
            )
            result.data = {
                "items": (
                    [event]
                    if REMINDER_MARKER in str(event.get("description") or "")
                    else []
                )
            }
        result.intent = "reminder.query"
        result.message = "提醒查询完成。"
        return result

    async def prepare_cancel(
        self,
        request: UserRequest,
        arguments: dict,
    ) -> ApplicationResult:
        return await self.calendar.prepare_delete(
            request,
            arguments,
            action_type="reminder.cancel",
        )
