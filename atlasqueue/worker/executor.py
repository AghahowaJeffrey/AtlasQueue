"""Job executor — run the handler and handle status transitions.

Status transitions implemented here
-------------------------------------
    running → succeeded    : handler returned without raising
    running → failed        : handler raised, attempts < max_attempts
                              (sets run_at = backoff timestamp)
    running → dead          : handler raised, attempts >= max_attempts
                              (pushes job_id to DLQ in Redis)

Each transition is a single ``UPDATE ... WHERE id = :id AND status = 'running'``
inside its own ``session.begin()`` block.  The ``AND status = 'running'``
guard prevents accidental double-transitions if a lease expires mid-execution
and another worker claims the job before we finish.

Unregistered job types
-----------------------
If no handler is found for ``job.type`` the job is failed/dead just like
any other handler error — the error message will say ``HandlerNotFound``.
This surfaces configuration problems quickly rather than silently dropping jobs.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

import redis.asyncio as aioredis
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from atlasqueue.core.config import Settings, get_settings
from atlasqueue.core.enums import JobStatus
from atlasqueue.core.logging import get_logger
from atlasqueue.core.metrics import (
    JOBS_DEAD,
    JOBS_FAILED,
    JOBS_SUCCEEDED,
    JOB_DURATION,
    WORKER_ACTIVE_JOBS,
)
from atlasqueue.db.models import Job
from atlasqueue.worker.handlers import get_handler
from atlasqueue.worker.retry import compute_backoff

_log = get_logger(__name__)
_settings = get_settings()


class HandlerNotFound(Exception):
    """Raised when no handler is registered for the given job type."""


# ---------------------------------------------------------------------------
# Status-transition helpers
# ---------------------------------------------------------------------------

async def _set_succeeded(session: AsyncSession, job_id: uuid.UUID) -> None:
    now = datetime.now(tz=timezone.utc)
    await session.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == JobStatus.RUNNING)
        .values(
            status=JobStatus.SUCCEEDED,
            locked_until=None,
            lock_owner=None,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    _log.info("job_succeeded", job_id=str(job_id))


async def _set_failed(
    session: AsyncSession,
    job: Job,
    error: str,
    settings: Settings,
) -> None:
    """Transition to ``failed`` and store the backoff ``run_at``."""
    run_at = compute_backoff(job.attempts, settings)
    now = datetime.now(tz=timezone.utc)
    await session.execute(
        update(Job)
        .where(Job.id == job.id, Job.status == JobStatus.RUNNING)
        .values(
            status=JobStatus.FAILED,
            last_error=error,
            run_at=run_at,
            locked_until=None,
            lock_owner=None,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    _log.warning(
        "job_failed",
        job_id=str(job.id),
        job_type=job.type,
        attempt=job.attempts,
        max_attempts=job.max_attempts,
        retry_at=run_at.isoformat(),
        error=error,
    )


async def _set_dead(
    session: AsyncSession,
    redis: aioredis.Redis,
    job: Job,
    error: str,
    settings: Settings,
) -> None:
    """Transition to ``dead`` and push job_id to the DLQ in Redis."""
    now = datetime.now(tz=timezone.utc)
    await session.execute(
        update(Job)
        .where(Job.id == job.id, Job.status == JobStatus.RUNNING)
        .values(
            status=JobStatus.DEAD,
            last_error=error,
            locked_until=None,
            lock_owner=None,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    await redis.lpush(settings.dlq_key, str(job.id))
    _log.error(
        "job_dead",
        job_id=str(job.id),
        job_type=job.type,
        attempt=job.attempts,
        max_attempts=job.max_attempts,
        dlq_key=settings.dlq_key,
        error=error,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def execute_job(
    session: AsyncSession,
    redis: aioredis.Redis,
    job: Job,
    settings: Settings | None = None,
) -> None:
    """Run the registered handler for *job* and update status accordingly.

    Must be called after a successful ``acquire_lease()`` — the job is
    expected to be in ``status=running``.

    Parameters
    ----------
    session:  Open AsyncSession.  A new ``session.begin()`` is started
              inside this function for the status-update phase.
    redis:    Shared aioredis client (used for DLQ LPUSH).
    job:      Job ORM object returned by ``acquire_lease()``.
    settings: App settings; defaults to ``get_settings()`` if omitted.
    """
    cfg = settings or _settings
    handler = get_handler(job.type)

    error: str | None = None
    WORKER_ACTIVE_JOBS.inc()
    t_start = time.perf_counter()
    try:
        if handler is None:
            raise HandlerNotFound(
                f"No handler registered for job type {job.type!r}"
            )
        _log.info(
            "handler_start",
            job_id=str(job.id),
            job_type=job.type,
            attempt=job.attempts,
        )
        await handler(job.payload)
        _log.info("handler_done", job_id=str(job.id), job_type=job.type)

    except Exception as exc:  # noqa: BLE001
        # Truncate here — before error is stored in DB or passed to helpers.
        raw = f"{type(exc).__name__}: {exc}"
        error = raw[:4096]
        _log.error(
            "handler_error",
            job_id=str(job.id),
            job_type=job.type,
            error=error,
        )
    finally:
        WORKER_ACTIVE_JOBS.dec()

    # ── Status transition + metrics ────────────────────────────────────
    duration = time.perf_counter() - t_start
    async with session.begin():
        if error is None:
            outcome = "succeeded"
            await _set_succeeded(session, job.id)
            JOBS_SUCCEEDED.labels(job_type=job.type).inc()
        elif job.attempts >= job.max_attempts:
            outcome = "dead"
            await _set_dead(session, redis, job, error, cfg)
            JOBS_DEAD.labels(job_type=job.type).inc()
        else:
            outcome = "failed"
            await _set_failed(session, job, error, cfg)
            JOBS_FAILED.labels(job_type=job.type).inc()

    JOB_DURATION.labels(job_type=job.type, outcome=outcome).observe(duration)
