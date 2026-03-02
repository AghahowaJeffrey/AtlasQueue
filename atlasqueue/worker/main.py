"""Worker entry point.

Run via:
    python -m atlasqueue.worker.main
or (in Docker):
    CMD ["python", "-m", "atlasqueue.worker.main"]

Observability
-------------
Starts a Prometheus metrics HTTP server on ``WORKER_METRICS_PORT`` (default 9091)
in a background thread before the async event loop starts.  The metrics endpoint
is then accessible at ``http://<worker-host>:9091/``.
"""
from __future__ import annotations

import asyncio

import redis.asyncio as aioredis
from prometheus_client import start_http_server

from atlasqueue.core.config import get_settings
from atlasqueue.core.logging import configure_logging, get_logger
from atlasqueue.worker.consumer import run_forever

_settings = get_settings()
configure_logging(log_level=_settings.log_level, service="atlasqueue-worker")
_log = get_logger(__name__)


async def main() -> None:
    # Start Prometheus sidecar before the event loop gets busy.
    start_http_server(_settings.worker_metrics_port)
    _log.info(
        "worker_started",
        redis_url=_settings.redis_url,
        lease_seconds=_settings.worker_lease_seconds,
        metrics_port=_settings.worker_metrics_port,
    )

    redis_client = aioredis.from_url(
        _settings.redis_url,
        decode_responses=True,
        max_connections=5,
    )
    try:
        await run_forever(redis_client)
    finally:
        await redis_client.aclose()
        _log.info("worker_stopped")


if __name__ == "__main__":
    asyncio.run(main())
