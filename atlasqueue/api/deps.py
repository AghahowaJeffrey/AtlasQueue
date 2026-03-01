"""FastAPI dependency providers — DB session and Redis client."""
from __future__ import annotations

from collections.abc import AsyncGenerator

import redis.asyncio as aioredis
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from atlasqueue.db.session import get_session


async def get_db(request: Request) -> AsyncGenerator[AsyncSession, None]:
    """Yield a transactional async DB session."""
    async for session in get_session():
        yield session


async def get_redis(request: Request) -> aioredis.Redis:
    """Return the shared Redis client stored in app state."""
    return request.app.state.redis
