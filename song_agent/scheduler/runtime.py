"""持久化调度器，支持 SQLite 领导租约和栅栏令牌。"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from ..config import Settings
from ..feishu.transport import FeishuTransport
from ..models import ScheduledJob
from ..store import SqliteStore
from .lease import SchedulerLease


async def start_scheduler(
    settings: Settings,
    store: SqliteStore,
    transport: FeishuTransport,
) -> AsyncIOScheduler:
    """启动轮询调度器，任务状态可在进程重启后保留。"""

    logger = logging.getLogger(__name__)
    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    lease = SchedulerLease(
        store,
        lease_name="scheduler",
        holder_id=f"scheduler:{uuid.uuid4()}",
    )
    definitions = (
        (
            "morning",
            settings.morning_cron,
            "⏰ 早上好！告诉我你今天的安排，我来整理你自己的计划。",
        ),
        (
            "evening",
            settings.evening_cron,
            "🌙 该做今天的复盘啦！告诉我你完成了什么；未提到的任务不会被猜测。",
        ),
    )
    for job_id, expression, text in definitions:
        await store.upsert_scheduled_job(
            job_id=job_id,
            job_type="p2p.broadcast",
            payload={"text": text},
            timezone=settings.timezone,
            cron_expression=expression,
            run_at=_next_fire_timestamp(expression, settings.timezone),
            app_id=settings.feishu_app_id,
        )

    async def execute_job(job: ScheduledJob, fencing_token: int) -> None:
        try:
            if job.job_type != "p2p.broadcast":
                raise RuntimeError(f"unsupported scheduled job: {job.job_type}")
            text = str(job.payload.get("text") or "")
            if not text:
                raise RuntimeError("scheduled broadcast text is empty")
            for user_id, chat_id in (
                await store.p2p_chat_ids(
                    tenant_key=job.tenant_key,
                    app_id=job.app_id,
                )
            ).items():
                if not await lease.is_current(fencing_token):
                    logger.warning("Scheduler fencing token 已失效，中止任务 %s", job.job_id)
                    return
                try:
                    await transport.send_markdown(chat_id, text)
                except Exception:
                    logger.exception("向用户 %s 发送定时提示失败", user_id)
                    raise
            next_run = (
                _next_fire_timestamp(job.cron_expression, job.timezone)
                if job.cron_expression
                else None
            )
            if not await store.complete_scheduled_job(
                job.job_id,
                holder_id=lease.holder_id,
                fencing_token=fencing_token,
                next_run_at=next_run,
            ):
                logger.warning("任务 %s 完成时 fencing token 已失效", job.job_id)
        except Exception as error:
            retry_delay = min(300, 2 ** min(job.attempts, 8))
            await store.fail_scheduled_job(
                job.job_id,
                holder_id=lease.holder_id,
                fencing_token=fencing_token,
                error=str(error),
                retry_delay_seconds=retry_delay,
            )

    async def tick() -> None:
        fencing_token = await lease.acquire()
        if fencing_token is None:
            return
        jobs = await store.claim_due_scheduled_jobs(
            lease_name=lease.lease_name,
            holder_id=lease.holder_id,
            fencing_token=fencing_token,
        )
        for job in jobs:
            if not await lease.is_current(fencing_token):
                logger.warning("Scheduler fencing token 已失效，中止本轮执行")
                return
            await execute_job(job, fencing_token)

    scheduler.add_job(
        tick,
        IntervalTrigger(seconds=settings.scheduler_poll_seconds),
        id="persistent-job-poller",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    await tick()
    return scheduler


def _next_fire_timestamp(expression: str, timezone: str) -> int:
    zone = ZoneInfo(timezone)
    now = datetime.now(zone)
    trigger = CronTrigger.from_crontab(expression, timezone=zone)
    next_fire = trigger.get_next_fire_time(None, now)
    if next_fire is None:
        raise ValueError(f"cron expression has no next fire time: {expression}")
    return max(int(next_fire.timestamp()), int(time.time()) + 1)
