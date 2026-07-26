"""时间上下文生成。时间字段最终由 Pydantic 与业务策略校验。"""

from datetime import datetime
from zoneinfo import ZoneInfo


def current_time_context(timezone: str) -> str:
    return datetime.now(ZoneInfo(timezone)).isoformat(timespec="seconds")
