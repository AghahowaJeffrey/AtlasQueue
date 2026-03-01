"""Job submission and retrieval endpoints — skeleton (M1).

Business logic (idempotency check, DB insert, Redis enqueue, lease
acquisition) is implemented in Milestone 2.
"""
from __future__ import annotations

from fastapi import APIRouter, Response, status

router = APIRouter(prefix="/v1/jobs", tags=["jobs"])


@router.post(
    "",
    status_code=status.HTTP_501_NOT_IMPLEMENTED,
    summary="Submit a job (not yet implemented)",
)
async def create_job(response: Response) -> dict[str, str]:
    response.status_code = status.HTTP_501_NOT_IMPLEMENTED
    return {"detail": "not implemented — coming in Milestone 2"}


@router.get(
    "/{job_id}",
    status_code=status.HTTP_501_NOT_IMPLEMENTED,
    summary="Retrieve a job (not yet implemented)",
)
async def get_job(job_id: str, response: Response) -> dict[str, str]:
    response.status_code = status.HTTP_501_NOT_IMPLEMENTED
    return {"detail": "not implemented — coming in Milestone 2"}
