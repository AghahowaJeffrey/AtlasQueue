"""GET /health — lightweight liveness probe.

Returns 200 immediately. No DB or Redis checks — those belong in a readiness
probe (added in M2 alongside the full service layer).
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health", summary="Liveness probe")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "atlasqueue-api"}
