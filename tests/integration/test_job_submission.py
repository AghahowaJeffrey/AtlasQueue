"""Integration tests — POST /v1/jobs and GET /v1/jobs/{job_id}.

These tests exercise the full request → router → service → (mocked) DB/Redis
path. No real database or Redis is required.

Test matrix
-----------
POST /v1/jobs
    ✓ New job: Idempotency-Key not seen before → 201, job_id in response
    ✓ Idempotent: same Idempotency-Key → 200, same job_id returned
    ✓ Missing Idempotency-Key header → 422 Unprocessable Entity
    ✓ Invalid body (missing required field) → 422

GET /v1/jobs/{job_id}
    ✓ Known job_id → 200 + full job object
    ✓ Unknown job_id → 404 Not Found
    ✓ Malformed UUID → 422
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from httpx import AsyncClient

from tests.conftest import make_idempotency_key, make_job

pytestmark = pytest.mark.asyncio


# ===========================================================================
# POST /v1/jobs
# ===========================================================================

class TestSubmitJob:
    """Happy path and idempotency for POST /v1/jobs."""

    async def test_new_job_returns_201(
        self,
        client: AsyncClient,
        mock_session: MagicMock,
        mock_redis: AsyncMock,
    ) -> None:
        """A fresh Idempotency-Key creates a new job and returns 201."""
        # No existing idempotency key in DB.
        mock_session.get.return_value = None

        resp = await client.post(
            "/v1/jobs",
            json={"type": "email.send", "payload": {"to": "alice@example.com"}},
            headers={"Idempotency-Key": "req-001"},
        )

        assert resp.status_code == 201
        body = resp.json()
        assert body["status"] == "queued"
        assert body["type"] == "email.send"
        assert body["attempts"] == 0
        assert uuid.UUID(body["id"])  # valid UUID

        # Service must have pushed the job_id to Redis once.
        mock_redis.lpush.assert_called_once()
        _, pushed_value = mock_redis.lpush.call_args.args
        assert str(uuid.UUID(pushed_value)) == pushed_value  # valid UUID string

    async def test_idempotent_submission_returns_200_same_job(
        self,
        client: AsyncClient,
        mock_session: MagicMock,
        mock_redis: AsyncMock,
    ) -> None:
        """Repeating the same Idempotency-Key returns 200 with the original job."""
        existing_job = make_job(job_type="email.send")
        existing_key = make_idempotency_key("req-dup", existing_job.id)

        # First get() call returns the IdempotencyKey; second returns the Job.
        mock_session.get.side_effect = [existing_key, existing_job]

        resp = await client.post(
            "/v1/jobs",
            json={"type": "email.send", "payload": {"to": "alice@example.com"}},
            headers={"Idempotency-Key": "req-dup"},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == str(existing_job.id)
        assert body["status"] == "queued"

        # No Redis push for duplicate.
        mock_redis.lpush.assert_not_called()

    async def test_missing_idempotency_key_returns_422(
        self,
        client: AsyncClient,
    ) -> None:
        """Omitting the Idempotency-Key header is a validation error."""
        resp = await client.post(
            "/v1/jobs",
            json={"type": "email.send", "payload": {}},
            # No Idempotency-Key header
        )
        assert resp.status_code == 422

    async def test_missing_job_type_returns_422(
        self,
        client: AsyncClient,
    ) -> None:
        """``type`` is required in the request body."""
        resp = await client.post(
            "/v1/jobs",
            json={"payload": {"key": "val"}},  # missing `type`
            headers={"Idempotency-Key": "req-003"},
        )
        assert resp.status_code == 422

    async def test_custom_max_attempts(
        self,
        client: AsyncClient,
        mock_session: MagicMock,
        mock_redis: AsyncMock,
    ) -> None:
        """max_attempts from the request body flows through to the response."""
        mock_session.get.return_value = None

        resp = await client.post(
            "/v1/jobs",
            json={"type": "sms.send", "payload": {}, "max_attempts": 3},
            headers={"Idempotency-Key": "req-004"},
        )

        assert resp.status_code == 201
        assert resp.json()["max_attempts"] == 3

    async def test_max_attempts_out_of_range_returns_422(
        self,
        client: AsyncClient,
    ) -> None:
        """max_attempts must be between 1 and 50 (schema validation)."""
        resp = await client.post(
            "/v1/jobs",
            json={"type": "sms.send", "payload": {}, "max_attempts": 100},
            headers={"Idempotency-Key": "req-005"},
        )
        assert resp.status_code == 422


# ===========================================================================
# GET /v1/jobs/{job_id}
# ===========================================================================

class TestGetJob:
    """Retrieval endpoint tests."""

    async def test_existing_job_returns_200(
        self,
        client: AsyncClient,
        mock_session: MagicMock,
    ) -> None:
        """GET a known job_id returns 200 with the job details."""
        job = make_job(job_type="report.generate", attempts=2)
        mock_session.get.return_value = job

        resp = await client.get(f"/v1/jobs/{job.id}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == str(job.id)
        assert body["type"] == "report.generate"
        assert body["attempts"] == 2
        assert body["status"] == "queued"

    async def test_unknown_job_returns_404(
        self,
        client: AsyncClient,
        mock_session: MagicMock,
    ) -> None:
        """GET an unknown job_id returns 404."""
        mock_session.get.return_value = None

        resp = await client.get(f"/v1/jobs/{uuid.uuid4()}")

        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    async def test_malformed_uuid_returns_422(
        self,
        client: AsyncClient,
    ) -> None:
        """A non-UUID path param is a validation error."""
        resp = await client.get("/v1/jobs/not-a-uuid")
        assert resp.status_code == 422

    async def test_last_error_field_present_in_response(
        self,
        client: AsyncClient,
        mock_session: MagicMock,
    ) -> None:
        """last_error is included in the response (can be null or a string)."""
        job = make_job(last_error="connection timeout")
        mock_session.get.return_value = job

        resp = await client.get(f"/v1/jobs/{job.id}")

        assert resp.status_code == 200
        assert resp.json()["last_error"] == "connection timeout"


# ===========================================================================
# Health smoke-test (sanity)
# ===========================================================================

async def test_health(client: AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "service": "atlasqueue-api"}
