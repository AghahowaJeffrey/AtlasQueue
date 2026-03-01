"""Worker Redis consumer — BLPOP polling loop.

Behaviour (M1 skeleton):
    - Blocks on BLPOP with a configurable timeout.
    - On timeout  → logs "no_jobs_waiting" and loops.
    - On job_id   → logs "job_received" with the raw value.
    - Handler execution is implemented in Milestone 2.

The loop runs until the process receives SIGTERM/SIGINT and the asyncio
event loop is cancelled.
"""
from __future__ import annotations

import asyncio
import signal

import redis.asyncio as aioredis

from atlasqueue.core.config import get_settings
from atlasqueue.core.logging import get_logger

_log = get_logger(__name__)
_settings = get_settings()

_SHUTDOWN = asyncio.Event()


def _install_signal_handlers() -> None:
    """Trigger graceful shutdown on SIGTERM or SIGINT."""

    def _handle(sig: signal.Signals) -> None:
        _log.info("signal_received", signal=sig.name)
        _SHUTDOWN.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _handle, sig)


async def run_forever(redis_client: aioredis.Redis) -> None:
    """Poll Redis indefinitely until shutdown is requested."""
    _install_signal_handlers()
    queue_key = _settings.job_queue_key
    timeout = _settings.worker_poll_timeout

    _log.info("consumer_started", queue=queue_key, poll_timeout_seconds=timeout)

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
        _log.info("job_received", job_id=raw_job_id)
        # TODO(M2): dispatch to handler registry.

    _log.info("consumer_stopped")
