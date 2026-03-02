"""Worker Redis consumer — BLPOP polling loop + lease acquisition + execution.

Full per-job flow
-----------------
1. BLPOP job_id from Redis.
2. Parse UUID; malformed → warn + skip.
3. Open a DB session; call ``acquire_lease()`` (atomic UPDATE).
   - 0 rows updated → skip (already claimed / not claimable).
4. Open a second DB session; call ``execute_job()`` which:
   - Runs the registered handler.
   - Transitions status: succeeded | failed (with backoff run_at) | dead (+ DLQ).
5. On BLPOP timeout → ``sweep_failed_jobs()`` re-enqueues due retries.

Graceful shutdown
-----------------
SIGTERM / SIGINT set ``_SHUTDOWN`` — checked between loop iterations.
In-flight DB sessions complete the current operation before the process exits.
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
from atlasqueue.worker.executor import execute_job
from atlasqueue.worker.lease import acquire_lease
from atlasqueue.worker.retry import sweep_failed_jobs

_log = get_logger(__name__)
_settings = get_settings()

_SHUTDOWN = asyncio.Event()

# Stable worker identity recorded in jobs.lock_owner for debuggability.
_WORKER_ID: str = f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"


def _install_signal_handlers() -> None:
    """Trigger graceful shutdown on SIGTERM or SIGINT."""

    def _handle(sig: signal.Signals) -> None:
        _log.info("signal_received", signal=sig.name)
        _SHUTDOWN.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _handle, sig)


async def _process_job_id(raw_job_id: str, redis_client: aioredis.Redis) -> None:
    """Acquire lease for *raw_job_id* and execute the handler.

    Isolated in its own coroutine so a failure on one job cannot crash
    the polling loop.
    """
    # ── Parse ─────────────────────────────────────────────────────────────
    try:
        job_id = uuid.UUID(raw_job_id)
    except ValueError:
        _log.error("invalid_job_id_format", raw=raw_job_id)
        return

    _log.info("job_received", job_id=str(job_id))

    try:
        # ── Lease acquisition (own session + transaction) ──────────────────
        job = None
        async with _session_factory() as session:
            async with session.begin():
                job = await acquire_lease(
                    session,
                    job_id,
                    _WORKER_ID,
                    _settings.worker_lease_seconds,
                )

        if job is None:
            return  # acquire_lease already logged the reason

        _log.info(
            "job_claimed",
            job_id=str(job.id),
            job_type=job.type,
            attempt=job.attempts,
            max_attempts=job.max_attempts,
            worker_id=_WORKER_ID,
        )

        # ── Handler execution (own session; executor manages its own begin()) ──
        async with _session_factory() as session:
            await execute_job(session, redis_client, job, _settings)

    except Exception as exc:  # noqa: BLE001
        _log.error(
            "job_processing_error",
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
            result = await redis_client.blpop(queue_key, timeout=timeout)
        except (aioredis.RedisError, OSError) as exc:
            _log.error("redis_error", error=str(exc), exc_info=True)
            await asyncio.sleep(1)
            continue

        if result is None:
            # Sweep failed jobs whose run_at has arrived back onto the queue.
            _log.debug("no_jobs_waiting", queue=queue_key)
            try:
                async with _session_factory() as session:
                    count = await sweep_failed_jobs(session, redis_client, _settings)
                    if count:
                        _log.info("sweep_requeued", count=count)
            except Exception as exc:  # noqa: BLE001
                _log.error("sweep_error", error=str(exc), exc_info=True)
            continue

        _, raw_job_id = result
        await _process_job_id(raw_job_id, redis_client)

    _log.info("consumer_stopped")
