"""AtlasQueue FastAPI application factory.

Lifespan:
    - Opens a shared async Redis connection and stores it in ``app.state.redis``.
    - Disposes Redis on shutdown.

Observability:
    - Structured JSON logs via structlog (configured at module level).
    - Prometheus metrics mounted at ``/metrics`` via the official ASGI app.
    - Per-request HTTP latency + count tracking via Starlette middleware.

The DB engine is managed by SQLAlchemy's connection pool and does not need
explicit lifecycle management here.
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

import redis.asyncio as aioredis
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import make_asgi_app

from atlasqueue.api.routers import health, jobs
from atlasqueue.core.config import get_settings
from atlasqueue.core.logging import configure_logging, get_logger
from atlasqueue.core.metrics import HTTP_REQUEST_DURATION, HTTP_REQUESTS_TOTAL

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

    # ── CORS ──────────────────────────────────────────────────────────────
    # Allow all origins for local dev; tighten in production.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── HTTP Metrics middleware ────────────────────────────────────────────
    @app.middleware("http")
    async def _track_request_metrics(request: Request, call_next) -> Response:
        # Use the route path template (/v1/jobs/{job_id}) not the concrete
        # URL (/v1/jobs/abc-123) to keep cardinality low.
        path_template = request.url.path
        route = request.scope.get("route")
        if route and hasattr(route, "path"):
            path_template = route.path

        start = time.perf_counter()
        response: Response = await call_next(request)
        duration = time.perf_counter() - start

        labels = {
            "method": request.method,
            "path_template": path_template,
            "status_code": str(response.status_code),
        }
        HTTP_REQUEST_DURATION.labels(**labels).observe(duration)
        HTTP_REQUESTS_TOTAL.labels(**labels).inc()

        return response

    # ── Routers ───────────────────────────────────────────────────────────
    app.include_router(health.router)
    app.include_router(jobs.router)

    # ── Prometheus metrics endpoint ───────────────────────────────────────
    metrics_app = make_asgi_app()
    app.mount("/metrics", metrics_app)

    return app


app = create_app()
