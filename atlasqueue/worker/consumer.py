"""Worker Redis consumer — BLPOP polling loop + lease acquisition.

Flow per received job
---------------------
1. BLPOP job_id from Redis.
2. Parse UUID; malformed → warn + skip (defensive).
3. Open a DB session and call ``acquire_lease()``:
   - UPDATE ... WHERE ... RETURNING — atomic, no TOCTOU race.
   - If 0 rows updated → log warning, skip (already claimed / not claimable).
   - If 1 row updated  → job is now ``status=running`` with a visibility
     timeout (``locked_until``).
4. TODO(M3): dispatch to handler registry; update status to succeeded/failed.

Graceful shutdown
-----------------
SIGTERM / SIGINT set ``_SHUTDOWN`` which is checked between loop iterations.
In-flight DB sessions complete their current work before the process exits.
"""
from __future__ import annotations

import asyncio
import signal
import socket
import uuid

import redis.asyncio as aioredis

from atlasqueue.core.config import get_settings
from atlasqueue.core.logging import get_logger
from atlasqueue.db.session import async_session as _session_factory
from atlasqueue.worker.lease import acquire_lease

_log = get_logger(__name__)
_settings = get_settings()

_SHUTDOWN = asyncio.Event()

# Stable worker identity — survives restarts only if hostname is stable.
# Recorded in jobs.lock_owner for observability / debugging.
_WORKER_ID: str = f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"


def _install_signal_handlers() -> None:
    """Trigger graceful shutdown on SIGTERM or SIGINT."""

    def _handle(sig: signal.Signals) -> None:
        _log.info("signal_received", signal=sig.name)
        _SHUTDOWN.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _handle, sig)


async def _process_job_id(raw_job_id: str) -> None:
    """Attempt to acquire the lease for *raw_job_id* and log the outcome.

    Isolated in its own coroutine so a DB error on one job does not crash
    the whole polling loop.
    """
    # ── Parse ─────────────────────────────────────────────────────────────
    try:
        job_id = uuid.UUID(raw_job_id)
    except ValueError:
        _log.error("invalid_job_id_format", raw=raw_job_id)
        return

    _log.info("job_received", job_id=str(job_id))

    # ── Lease acquisition ─────────────────────────────────────────────────
    try:
        async with _session_factory() as session:
            async with session.begin():
                job = await acquire_lease(
                    session,
                    job_id,
                    _WORKER_ID,
                    _settings.worker_lease_seconds,
                )

        if job is None:
            # Not claimable: another worker won the race, or the job has
            # already been processed / is scheduled for the future.
            return

        _log.info(
            "job_claimed",
            job_id=str(job.id),
            job_type=job.type,
            attempt=job.attempts,
            max_attempts=job.max_attempts,
            worker_id=_WORKER_ID,
        )

        # TODO(M3): dispatch to handler registry, then:
        #   On success → mark succeeded
        #   On failure → mark failed / compute backoff / push to DLQ

    except Exception as exc:  # noqa: BLE001
        _log.error(
            "lease_error",
            job_id=str(job_id),
            error=str(exc),
            exc_info=True,
        )


async def run_forever(redis_client: aioredis.Redis) -> None:
    """Poll Redis indefinitely until shutdown is requested."""
    _install_signal_handlers()
    queue_key = _settings.job_queue_key
    timeout = _settings.worker_poll_timeout

    _log.info(
        "consumer_started",
        queue=queue_key,
        poll_timeout_seconds=timeout,
        worker_id=_WORKER_ID,
    )

    while not _SHUTDOWN.is_set():
        try:
            # BLPOP returns (key, value) or None on timeout.
            result = await redis_client.blpop(queue_key, timeout=timeout)
        except (aioredis.RedisError, OSError) as exc:
            _log.error("redis_error", error=str(exc), exc_info=True)
            await asyncio.sleep(1)  # back off briefly before retrying
            continue

        if result is None:
            _log.debug("no_jobs_waiting", queue=queue_key)
            continue

        _, raw_job_id = result
        await _process_job_id(raw_job_id)

    _log.info("consumer_stopped")

