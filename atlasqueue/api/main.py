"""AtlasQueue FastAPI application factory.

Lifespan:
    - Opens a shared async Redis connection and stores it in ``app.state.redis``.
    - Disposes Redis on shutdown.

The DB engine is managed by SQLAlchemy's connection pool and does not need
explicit lifecycle management here.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from atlasqueue.api.routers import health, jobs
from atlasqueue.core.config import get_settings
from atlasqueue.core.logging import configure_logging, get_logger

_settings = get_settings()
configure_logging(log_level=_settings.log_level, service="atlasqueue-api")

_log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage application-scoped resources across the request lifecycle."""
    _log.info("startup", redis_url=_settings.redis_url)
    app.state.redis = aioredis.from_url(
        _settings.redis_url,
        decode_responses=True,
        max_connections=20,
    )
    yield
    _log.info("shutdown")
    await app.state.redis.aclose()


def create_app() -> FastAPI:
    app = FastAPI(
        title="AtlasQueue",
        description="Distributed task processing engine — v1",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # Allow all origins for local dev; tighten in production.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(jobs.router)

    return app


app = create_app()
