import asyncio
import time
from pathlib import Path

import pytest

from song_agent.services.encryption import AesGcmTokenCipher
from song_agent.store import SqliteStore


async def store(path: Path) -> SqliteStore:
    value = SqliteStore(
        path,
        app_id="app",
        token_cipher=AesGcmTokenCipher({1: b"1" * 32}, 1),
    )
    await value.initialize()
    return value


@pytest.mark.asyncio
async def test_scheduler_lease_takeover_invalidates_old_fencing_token(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.db"
    first = await store(path)
    second = await store(path)
    try:
        token_one = await first.acquire_scheduler_lease(
            "scheduler", "worker-one", lease_seconds=60
        )
        assert token_one == 1
        assert (
            await second.acquire_scheduler_lease(
                "scheduler", "worker-two", lease_seconds=60
            )
            is None
        )
        await first.db.execute(
            "UPDATE scheduler_leases SET expires_at = 0 WHERE lease_name = 'scheduler'"
        )
        await first.db.commit()
        token_two = await second.acquire_scheduler_lease(
            "scheduler", "worker-two", lease_seconds=60
        )
        assert token_two == 2
        assert not await first.validate_scheduler_lease(
            "scheduler", "worker-one", token_one
        )
        assert await second.validate_scheduler_lease(
            "scheduler", "worker-two", token_two
        )
    finally:
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_only_one_scheduler_wins_concurrent_acquire(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    first = await store(path)
    second = await store(path)
    try:
        results = await asyncio.gather(
            first.acquire_scheduler_lease("scheduler", "one", lease_seconds=60),
            second.acquire_scheduler_lease("scheduler", "two", lease_seconds=60),
        )
        assert sum(value is not None for value in results) == 1
    finally:
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_due_job_is_persisted_claimed_and_rescheduled_with_fencing(
    tmp_path: Path,
) -> None:
    value = await store(tmp_path / "state.db")
    try:
        now = int(time.time())
        await value.upsert_scheduled_job(
            job_id="morning",
            job_type="p2p.broadcast",
            payload={"text": "hello"},
            timezone="Asia/Shanghai",
            cron_expression="0 8 * * *",
            run_at=now - 60,
            app_id="app",
        )
        token = await value.acquire_scheduler_lease(
            "scheduler",
            "leader",
            lease_seconds=60,
        )
        assert token == 1
        jobs = await value.claim_due_scheduled_jobs(
            lease_name="scheduler",
            holder_id="leader",
            fencing_token=token,
        )
        assert [job.job_id for job in jobs] == ["morning"]
        assert await value.claim_due_scheduled_jobs(
            lease_name="scheduler",
            holder_id="leader",
            fencing_token=token,
        ) == []
        assert await value.complete_scheduled_job(
            "morning",
            holder_id="leader",
            fencing_token=token,
            next_run_at=now + 86400,
        )
        row = await (
            await value.db.execute(
                "SELECT status, run_at, attempts FROM scheduled_jobs WHERE job_id = 'morning'"
            )
        ).fetchone()
        assert row["status"] == "scheduled"
        assert row["run_at"] == now + 86400
        assert row["attempts"] == 1
    finally:
        await value.close()


@pytest.mark.asyncio
async def test_failed_job_gets_persistent_retry_backoff(tmp_path: Path) -> None:
    value = await store(tmp_path / "state.db")
    try:
        await value.upsert_scheduled_job(
            job_id="retry-job",
            job_type="p2p.broadcast",
            payload={"text": "hello"},
            timezone="Asia/Shanghai",
            cron_expression="",
            run_at=0,
            app_id="app",
        )
        token = await value.acquire_scheduler_lease(
            "scheduler",
            "leader",
            lease_seconds=60,
        )
        jobs = await value.claim_due_scheduled_jobs(
            lease_name="scheduler",
            holder_id="leader",
            fencing_token=token or 0,
        )
        assert len(jobs) == 1
        before = int(time.time())
        assert await value.fail_scheduled_job(
            "retry-job",
            holder_id="leader",
            fencing_token=token or 0,
            error="temporary",
            retry_delay_seconds=30,
        )
        row = await (
            await value.db.execute(
                """
                SELECT status, next_retry_at, last_error
                FROM scheduled_jobs WHERE job_id = 'retry-job'
                """
            )
        ).fetchone()
        assert row["status"] == "retry"
        assert row["next_retry_at"] >= before + 30
        assert row["last_error"] == "temporary"
    finally:
        await value.close()
