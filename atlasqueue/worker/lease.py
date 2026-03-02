"""Lease acquisition — atomic UPDATE-based job claiming.

Strategy
--------
A single ``UPDATE jobs SET ... WHERE ... RETURNING *`` is the correct primitive
for lease acquisition in a multi-worker environment:

* **Atomic**: the WHERE + SET happen in one DB round-trip with no separate
  SELECT, eliminating the TOCTOU (time-of-check/time-of-use) race that a
  SELECT-then-UPDATE pattern would have.

* **No advisory locks needed**: if two workers race for the same job_id,
  Postgres row-level write conflict ensures only one UPDATE wins; the second
  worker's UPDATE matches 0 rows and returns ``None``.

* **SKIP LOCKED not needed**: we target by primary key, so there is no
  full-table scan to skip.  SELECT-FOR-UPDATE-SKIP-LOCKED is only needed
  when workers pull from an unfiltered queue scan.

Claim criteria (PRD §5.3)
--------------------------
* ``status IN ('queued', 'failed')``
* ``run_at <= now()``
* ``locked_until IS NULL OR locked_until < now()``

On success the row transitions to ``status='running'`` and ``attempts``
is incremented atomically.

On failure (0 rows updated) the caller receives ``None`` and should log a
warning — the job may have already been claimed by another worker, its
`run_at` may be in the future (retry backoff), or it may be in a terminal
state.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, update
from sqlalchemy.ext.asyncio import AsyncSession

from atlasqueue.core.enums import JobStatus
from atlasqueue.core.logging import get_logger
from atlasqueue.core.metrics import LEASE_ACQUIRED, LEASE_REJECTED
from atlasqueue.db.models import Job

_log = get_logger(__name__)


async def acquire_lease(
    session: AsyncSession,
    job_id: uuid.UUID,
    worker_id: str,
    lease_seconds: int,
) -> Job | None:
    """Atomically claim a job for this worker.

    Must be called within an active ``session.begin()`` context.

    Parameters
    ----------
    session:       An open AsyncSession (transaction must be started by caller).
    job_id:        Primary key of the job to claim.
    worker_id:     Unique string identifying this worker instance.
    lease_seconds: How long (in seconds) the lease is valid; sets
                   ``locked_until = now + lease_seconds``.

    Returns
    -------
    The updated ``Job`` ORM instance if the claim succeeded, ``None`` if the
    job was not claimable (already locked, wrong status, or ``run_at`` in future).
    """
    now = datetime.now(tz=timezone.utc)
    locked_until = now + timedelta(seconds=lease_seconds)

    stmt = (
        update(Job)
        .where(
            Job.id == job_id,
            # Only claimable statuses.
            Job.status.in_([JobStatus.QUEUED, JobStatus.FAILED]),
            # Scheduled time must have arrived.
            Job.run_at <= now,
            # Not currently leased (or lease has expired — crash recovery).
            or_(Job.locked_until.is_(None), Job.locked_until < now),
        )
        .values(
            status=JobStatus.RUNNING,
            locked_until=locked_until,
            lock_owner=worker_id,
            # Atomically increment attempt counter in the same statement.
            attempts=Job.attempts + 1,
            updated_at=now,
        )
        .returning(Job)
        # Prevents SQLAlchemy from trying to synchronise the identity map
        # with the UPDATE result (not supported for bulk updates in async).
        .execution_options(synchronize_session=False)
    )

    result = await session.scalars(stmt)
    job: Job | None = result.one_or_none()

    if job is not None:
        LEASE_ACQUIRED.inc()
        _log.info(
            "lease_acquired",
            job_id=str(job.id),
            job_type=job.type,
            attempt=job.attempts,
            locked_until=locked_until.isoformat(),
            worker_id=worker_id,
        )
    else:
        LEASE_REJECTED.inc()
        _log.warning(
            "lease_not_acquired",
            job_id=str(job_id),
            worker_id=worker_id,
            reason="not_claimable",
        )

    return job
