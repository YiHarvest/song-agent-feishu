"""
调度器模块。

使用 APScheduler 实现定时任务，支持早晚提醒广播。
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import Settings
from .feishu.transport import FeishuTransport
from .store import JsonStore


def start_scheduler(settings: Settings, store: JsonStore, transport: FeishuTransport) -> AsyncIOScheduler:
    """
    启动定时任务调度器。

    配置早晚提醒广播任务，向用户的私聊发送定时消息。

    Args:
        settings: 应用配置。
        store: 状态存储实例。
        transport: 飞书消息传输实例。

    Returns:
        已启动的调度器实例。
    """
    logger = logging.getLogger(__name__)
    scheduler = AsyncIOScheduler(timezone=settings.timezone)

    async def broadcast(text: str) -> None:
        # Scheduled nudges go to each user's own p2p chat to avoid group spam.
        for user_id, chat_id in store.p2p_chat_ids().items():
            try:
                await transport.send_markdown(chat_id, text)
            except Exception:
                logger.exception("向用户 %s 发送定时提示失败", user_id)

    scheduler.add_job(
        broadcast,
        CronTrigger.from_crontab(settings.morning_cron, timezone=settings.timezone),
        args=["⏰ 早上好！告诉我你今天的安排，我来整理你自己的计划。"],
        id="morning",
    )
    scheduler.add_job(
        broadcast,
        CronTrigger.from_crontab(settings.evening_cron, timezone=settings.timezone),
        args=["🌙 该做今天的复盘啦！告诉我你完成了什么；未提到的任务不会被猜测。"],
        id="evening",
    )
    scheduler.start()
    return scheduler
