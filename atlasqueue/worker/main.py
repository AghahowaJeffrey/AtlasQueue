"""Worker entry point.

Run via:
    python -m atlasqueue.worker.main
or (in Docker):
    CMD ["python", "-m", "atlasqueue.worker.main"]
"""
from __future__ import annotations

import asyncio

import redis.asyncio as aioredis

from atlasqueue.core.config import get_settings
from atlasqueue.core.logging import configure_logging, get_logger
from atlasqueue.worker.consumer import run_forever

_settings = get_settings()
configure_logging(log_level=_settings.log_level, service="atlasqueue-worker")
_log = get_logger(__name__)


async def main() -> None:
    _log.info(
        "worker_started",
        redis_url=_settings.redis_url,
        lease_seconds=_settings.worker_lease_seconds,
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
