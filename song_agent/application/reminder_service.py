"""提醒作为带 Song Agent 标记的飞书日程管理。"""

from __future__ import annotations

from pydantic import ValidationError

from ..domain.commands import CalendarCreateCommand
from ..domain.intents import UserRequest
from ..domain.policies import normalize_calendar_create
from ..domain.results import ApplicationResult
from .calendar_service import CalendarApplicationService

REMINDER_MARKER = "[song-agent:reminder]"


class ReminderApplicationService:
    def __init__(self, calendar: CalendarApplicationService) -> None:
        self.calendar = calendar

    def _marked_arguments(self, arguments: dict) -> dict:
        arguments = dict(arguments)
        description = str(arguments.get("description") or "")
        arguments["description"] = f"{REMINDER_MARKER}\n{description}".strip()
        arguments.setdefault("reminder_minutes", [0])
        return arguments

    async def create(
        self,
        request: UserRequest,
        arguments: dict,
    ) -> ApplicationResult:
        return await self.calendar.create(
            request,
            self._marked_arguments(arguments),
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

    async def create_batch(
        self,
        request: UserRequest,
        arguments: dict,
    ) -> ApplicationResult:
        raw_items = arguments.get("items")
        if not isinstance(raw_items, list) or len(raw_items) < 2:
            return ApplicationResult(
                status="clarification_required",
                intent="reminder.batch_create",
                message="批量提醒至少需要两项。",
            )
        try:
            items = [
                normalize_calendar_create(
                    CalendarCreateCommand.model_validate(item),
                    default_timezone=self.calendar.default_timezone,
                    default_duration_minutes=1,
                    repair_nonpositive_duration=True,
                ).model_dump(mode="json")
                for item in raw_items
            ]
        except (ValidationError, ValueError) as error:
            return ApplicationResult(
                status="clarification_required",
                intent="reminder.batch_create",
                message=str(error),
            )
        if any(item.get("attendee_open_ids") for item in items):
            # 任一项带参与人 → 整批确认，不做混合执行
            action_ids: list[str] = []
            for index, item in enumerate(items):
                item_request = request.model_copy(
                    update={"message_id": f"{request.message_id}:reminder:{index}"}
                )
                result = await self.calendar.prepare_create_confirmation(
                    item_request,
                    self._marked_arguments(item),
                    action_type="reminder.create",
                )
                if result.status != "awaiting_confirmation":
                    return result.model_copy(
                        update={"data": {**result.data, "action_ids": action_ids}}
                    )
                action_ids.append(result.action_id)
            return ApplicationResult(
                status="awaiting_confirmation",
                intent="reminder.batch_create",
                action_id=action_ids[0],
                message=f"已准备 {len(action_ids)} 个提醒，等待逐一确认。",
                data={"action_ids": action_ids},
            )
        created: list[dict] = []
        for index, item in enumerate(items):
            item_request = request.model_copy(
                update={"message_id": f"{request.message_id}:reminder:{index}"}
            )
            result = await self.create(item_request, item)
            if result.status != "ok":
                return result.model_copy(
                    update={"data": {**result.data, "created": created}}
                )
            created.append(result.data)
        return ApplicationResult(
            status="ok",
            intent="reminder.batch_create",
            message=f"✅ 已创建 {len(created)} 条提醒",
            data={"created": created},
        )

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
