"""FastAPI dependency providers — DB session and Redis client.

``get_db`` yields a *plain* (no auto-begin) session so that the service
layer can control transaction boundaries explicitly via ``session.begin()``.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator

import redis.asyncio as aioredis
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from atlasqueue.db.session import async_session


async def get_db(request: Request) -> AsyncGenerator[AsyncSession, None]:
    """Yield a plain async session — the service layer owns begin()/commit()."""
    async with async_session() as session:
        yield session


async def get_redis(request: Request) -> aioredis.Redis:
    """Return the shared Redis client stored in app state."""
    return request.app.state.redis
