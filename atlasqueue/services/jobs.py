"""Job service — core business logic for job submission and retrieval.

Design decisions
----------------
* **Service manages the transaction**: ``submit_job`` opens its own
  ``session.begin()`` block so the router can yield a plain session
  (no auto-begin), keeping service logic self-contained and testable.

* **Enqueue AFTER commit**: Redis LPUSH happens only after the DB
  transaction is committed.  If the LPUSH fails, the job remains in
  ``status=queued`` in Postgres and a future worker sweep (§5.3) can
  recover it — satisfying the at-least-once guarantee.

* **Idempotency check inside the transaction**: ``SELECT … FOR UPDATE``
  semantics via SQLAlchemy's ``session.get()`` within an explicit
  ``begin()`` block. Under ``READ COMMITTED`` (Postgres default), two
  concurrent submissions with the same key can both read NULL and both
  try to insert.  The ``PRIMARY KEY`` constraint on ``idempotency_keys``
  guarantees only one succeeds; the loser gets an ``IntegrityError``
  and the router should surface a 409 (added later) or re-read the
  winning record.  For v1 we let the IntegrityError propagate as a
  500 — explicitly noted as a v2 hardening item.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession

from atlasqueue.core.config import get_settings
from atlasqueue.core.enums import JobStatus
from atlasqueue.core.logging import get_logger
from atlasqueue.db.models import IdempotencyKey, Job

_settings = get_settings()
_log = get_logger(__name__)


async def submit_job(
    session: AsyncSession,
    redis: aioredis.Redis,
    *,
    job_type: str,
    payload: dict,
    max_attempts: int,
    idempotency_key: str,
) -> tuple[Job, bool]:
    """Submit a job, enforcing idempotency on *idempotency_key*.

    Returns:
        A ``(job, created)`` tuple.
        *created* is ``True`` for a freshly inserted job, ``False`` when an
        existing job was found for the given idempotency key.
    """
    job: Job | None = None
    created: bool = True

    async with session.begin():
        # 1. Idempotency check ── fast path for duplicate submissions.
        existing_key = await session.get(IdempotencyKey, idempotency_key)
        if existing_key is not None:
            job = await session.get(Job, existing_key.job_id)
            _log.info(
                "idempotent_submission",
                idempotency_key=idempotency_key,
                existing_job_id=str(existing_key.job_id),
            )
            created = False
        else:
            # 2. Atomically create job + idempotency record.
            now = datetime.now(tz=timezone.utc)
            job = Job(
                id=uuid.uuid4(),
                type=job_type,
                payload=payload,
                max_attempts=max_attempts,
                status=JobStatus.QUEUED,
                run_at=now,
                attempts=0,
                created_at=now,
                updated_at=now,
            )
            ikey = IdempotencyKey(key=idempotency_key, job_id=job.id)
            session.add(job)
            session.add(ikey)
            # flush → DB constraint checks + job.id materialised in memory.
            # Actual COMMIT happens when begin() exits normally.
            await session.flush()
            _log.info(
                "job_created",
                job_id=str(job.id),
                job_type=job_type,
                idempotency_key=idempotency_key,
            )
    # ── Transaction committed at this point ─────────────────────────────────

    # 3. Enqueue AFTER commit.  Recovery path: worker sweep over status=queued.
    if created:
        await redis.lpush(_settings.job_queue_key, str(job.id))
        _log.info("job_enqueued", job_id=str(job.id), queue=_settings.job_queue_key)

    return job, created  # type: ignore[return-value]


async def get_job(session: AsyncSession, job_id: uuid.UUID) -> Job | None:
    """Retrieve a single job by primary key. Returns ``None`` if not found."""
    async with session.begin():
        return await session.get(Job, job_id)
