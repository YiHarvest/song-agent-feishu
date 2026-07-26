"""确定性业务策略。"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .commands import CalendarCreateCommand


def normalize_calendar_create(
    command: CalendarCreateCommand,
    *,
    default_timezone: str,
    now: datetime | None = None,
    default_duration_minutes: int = 60,
    repair_nonpositive_duration: bool = False,
) -> CalendarCreateCommand:
    timezone = command.timezone or default_timezone or "Asia/Shanghai"
    zone = ZoneInfo(timezone)
    current = now.astimezone(zone) if now else datetime.now(zone)
    start = command.start_time
    if start is None:
        raise ValueError("缺少日程开始时间")
    start = start.replace(tzinfo=zone) if start.tzinfo is None else start.astimezone(zone)
    end = command.end_time or (
        start + timedelta(minutes=default_duration_minutes)
    )
    end = end.replace(tzinfo=zone) if end.tzinfo is None else end.astimezone(zone)
    if end <= start:
        if repair_nonpositive_duration:
            end = start + timedelta(minutes=default_duration_minutes)
        else:
            raise ValueError("日程结束时间必须晚于开始时间")
    if start < current:
        raise ValueError("不能创建已经开始的日程")
    return command.model_copy(
        update={"timezone": timezone, "start_time": start, "end_time": end}
    )
