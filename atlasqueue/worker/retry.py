"""Retry backoff computation and failed-job sweep.

Backoff formula (PRD §9)
------------------------
    delay = base_delay * (2 ^ attempts) + jitter
    jitter = Uniform(0, jitter_max)

The ``attempts`` value passed in should be the CURRENT attempt count
(already incremented by lease acquisition), so the backoff grows with
each failure.

Sweep logic
-----------
``sweep_failed_jobs`` is intended to be called periodically (e.g. on each
BLPOP timeout in the consumer loop).  It atomically transitions all
``status=failed`` jobs whose ``run_at`` has arrived back to ``status=queued``
and re-enqueues their IDs to Redis.

Crash safety
------------
If the process dies after ``LPUSH`` but before the DB ``UPDATE``, the job
remains ``failed`` with an expired ``run_at`` and the next sweep recovers it
(at-least-once guarantee).  A duplicate Redis entry causes at most one extra
lease attempt — the second worker's ``UPDATE`` matches 0 rows and skips
the job cleanly.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import redis.asyncio as aioredis
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from atlasqueue.core.config import Settings
from atlasqueue.core.enums import JobStatus
from atlasqueue.core.logging import get_logger
from atlasqueue.db.models import Job

_log = get_logger(__name__)


def compute_backoff(attempts: int, settings: Settings) -> datetime:
    """Return the absolute ``run_at`` timestamp for the next retry attempt.

    Parameters
    ----------
    attempts:
        Current attempt count (post-increment — the value stored in the DB
        after lease acquisition bumped it).
    settings:
        Application settings providing ``retry_base_delay_seconds`` and
        ``retry_jitter_max_seconds``.
    """
    delay_seconds = settings.retry_base_delay_seconds * (2 ** attempts)
    jitter_seconds = random.uniform(0, settings.retry_jitter_max_seconds)
    return datetime.now(tz=timezone.utc) + timedelta(
        seconds=delay_seconds + jitter_seconds
    )


async def sweep_failed_jobs(
    session: AsyncSession,
    redis: aioredis.Redis,
    settings: Settings,
) -> int:
    """Re-enqueue failed jobs whose ``run_at`` has arrived.

    Returns the number of jobs re-enqueued.

    Algorithm (crash-safe at-least-once)
    -------------------------------------
    1. SELECT ids WHERE status=failed AND run_at <= now.
    2. LPUSH each id to the main queue (Redis side-effect first).
    3. Bulk UPDATE status=queued (DB commitment last).

    If the process crashes between steps 2 and 3, the job receives a
    duplicate Redis entry — the lease machinery handles that gracefully.
    """
    now = datetime.now(tz=timezone.utc)

    async with session.begin():
        # Step 1 — SELECT with FOR UPDATE SKIP LOCKED to avoid racing
        # multiple workers in the sweep path.
        from sqlalchemy import select  # local to avoid circular

        result = await session.execute(
            select(Job.id)
            .where(
                Job.status == JobStatus.FAILED,
                Job.run_at <= now,
            )
            .with_for_update(skip_locked=True)
        )
        due_ids = list(result.scalars().all())

        if not due_ids:
            return 0

        # Step 2 — LPUSH all (outside the DB transaction is fine; we hold the
        # row lock so no other sweeper will pick these up simultaneously).
        for job_id in due_ids:
            await redis.lpush(settings.job_queue_key, str(job_id))
            _log.info("retry_requeued", job_id=str(job_id))

        # Step 3 — Bulk UPDATE to queued now that Redis entries are written.
        from sqlalchemy import tuple_, column  # local import for clarity
        import uuid

        await session.execute(
            update(Job)
            .where(Job.id.in_(due_ids))
            .values(status=JobStatus.QUEUED, updated_at=now)
            .execution_options(synchronize_session=False)
        )

    _log.info("sweep_complete", requeued=len(due_ids))
    return len(due_ids)
