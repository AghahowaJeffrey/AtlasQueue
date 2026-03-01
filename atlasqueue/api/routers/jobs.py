"""Job submission and retrieval endpoints.

POST /v1/jobs
    Requires ``Idempotency-Key`` header (string, ≤ 512 chars).
    201 Created  — new job was inserted.
    200 OK       — duplicate key; existing job returned as-is.

GET /v1/jobs/{job_id}
    200 OK       — job found.
    404 Not Found — unknown job_id.
"""
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

import redis.asyncio as aioredis

from atlasqueue.api.deps import get_db, get_redis
from atlasqueue.api.schemas import JobCreateRequest, JobResponse
from atlasqueue.services.jobs import get_job, submit_job

router = APIRouter(prefix="/v1/jobs", tags=["jobs"])


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=JobResponse,
    summary="Submit a job",
    description=(
        "Creates a durable job record and enqueues it for processing. "
        "Subsequent requests with the same ``Idempotency-Key`` return the "
        "original job without side-effects (200 OK)."
    ),
)
async def create_job(
    body: JobCreateRequest,
    response: Response,
    idempotency_key: Annotated[
        str,
        Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=512,
            description="Caller-supplied unique key to prevent duplicate submissions.",
        ),
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[aioredis.Redis, Depends(get_redis)],
) -> JobResponse:
    job, created = await submit_job(
        db,
        redis,
        job_type=body.type,
        payload=body.payload,
        max_attempts=body.max_attempts,
        idempotency_key=idempotency_key,
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    return JobResponse.model_validate(job)


@router.get(
    "/{job_id}",
    status_code=status.HTTP_200_OK,
    response_model=JobResponse,
    summary="Retrieve a job by ID",
)
async def get_job_endpoint(
    job_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JobResponse:
    job = await get_job(db, job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job {job_id} not found.",
        )
    return JobResponse.model_validate(job)

