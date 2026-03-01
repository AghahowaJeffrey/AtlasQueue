"""Pydantic v2 I/O schemas for the AtlasQueue API."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from atlasqueue.core.enums import JobStatus


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class JobCreateRequest(BaseModel):
    """Body of POST /v1/jobs."""

    type: str = Field(..., min_length=1, max_length=255, description="Handler type identifier")
    payload: dict[str, Any] = Field(default_factory=dict, description="Arbitrary job payload")
    max_attempts: int = Field(default=5, ge=1, le=50, description="Max retry attempts")


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class JobResponse(BaseModel):
    """Serialized job record returned to callers."""

    model_config = {"from_attributes": True}

    id: uuid.UUID
    type: str
    status: JobStatus
    attempts: int
    max_attempts: int
    last_error: str | None
    run_at: datetime
    created_at: datetime
    updated_at: datetime
