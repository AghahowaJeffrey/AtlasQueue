"""Shared pytest fixtures for AtlasQueue test suite.

Architecture
------------
* Tests run without any real infrastructure (no Postgres, no Redis).
* The FastAPI ``lifespan`` is patched so it never opens a real Redis
  connection — the ``aioredis.from_url`` call in ``api/main.py`` is
  replaced with an AsyncMock.
* ``get_db`` and ``get_redis`` dependencies are overridden via
  ``app.dependency_overrides`` so each test controls exactly what the
  service layer sees.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from atlasqueue.api.deps import get_db, get_redis
from atlasqueue.api.main import app
from atlasqueue.core.enums import JobStatus
from atlasqueue.db.models import IdempotencyKey, Job


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_job(
    *,
    job_id: uuid.UUID | None = None,
    job_type: str = "email.send",
    payload: dict | None = None,
    status: JobStatus = JobStatus.QUEUED,
    attempts: int = 0,
    max_attempts: int = 5,
    last_error: str | None = None,
) -> Job:
    """Return an in-memory ``Job`` ORM object (not attached to any session)."""
    now = datetime.now(tz=timezone.utc)
    return Job(
        id=job_id or uuid.uuid4(),
        type=job_type,
        payload=payload or {"to": "user@example.com"},
        status=status,
        attempts=attempts,
        max_attempts=max_attempts,
        run_at=now,
        locked_until=None,
        lock_owner=None,
        last_error=last_error,
        created_at=now,
        updated_at=now,
    )


def make_idempotency_key(key: str, job_id: uuid.UUID) -> IdempotencyKey:
    return IdempotencyKey(
        key=key,
        job_id=job_id,
        created_at=datetime.now(tz=timezone.utc),
    )


# ---------------------------------------------------------------------------
# Session mock
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_session() -> MagicMock:
    """Mock SQLAlchemy AsyncSession.

    Supports:
        ``session.get(model, pk)``  → configure via ``.get.return_value``
        ``async with session.begin():`` → no-op async context
        ``session.add(obj)``        → no-op
        ``session.flush()``         → no-op coroutine
    """
    session = MagicMock()

    # async session.get()
    session.get = AsyncMock(return_value=None)

    # session.add() is synchronous
    session.add = MagicMock()

    # async session.flush()
    session.flush = AsyncMock()

    # session.begin() must be usable as `async with session.begin():`
    begin_ctx = AsyncMock()
    begin_ctx.__aenter__ = AsyncMock(return_value=None)
    begin_ctx.__aexit__ = AsyncMock(return_value=None)
    session.begin = MagicMock(return_value=begin_ctx)

    return session


# ---------------------------------------------------------------------------
# Redis mock
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_redis() -> AsyncMock:
    """Mock aioredis.Redis — tracks lpush calls."""
    redis = AsyncMock()
    redis.lpush = AsyncMock(return_value=1)
    redis.aclose = AsyncMock()
    return redis


# ---------------------------------------------------------------------------
# HTTP test client
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def client(
    mock_session: MagicMock,
    mock_redis: AsyncMock,
) -> AsyncGenerator[AsyncClient, None]:
    """Async HTTP client wired to the FastAPI app with mocked infra.

    * ``get_db``    → yields ``mock_session``
    * ``get_redis`` → returns  ``mock_redis``
    * ``aioredis.from_url`` in lifespan → returns ``mock_redis``
    """

    async def _db_override():
        yield mock_session

    def _redis_override():
        return mock_redis

    app.dependency_overrides[get_db] = _db_override
    app.dependency_overrides[get_redis] = _redis_override

    # Patch the lifespan so it doesn't open a real Redis connection.
    with patch("atlasqueue.api.main.aioredis.from_url", return_value=mock_redis):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as ac:
            yield ac

    app.dependency_overrides.clear()
