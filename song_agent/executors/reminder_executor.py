"""提醒创建复用日历创建实现，但使用独立 action_type。"""

from .calendar_executor import CalendarCreateExecutor


class ReminderCreateExecutor(CalendarCreateExecutor):
    action_type = "reminder.create"
