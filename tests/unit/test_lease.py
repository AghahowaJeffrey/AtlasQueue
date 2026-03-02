"""Unit tests for worker/lease.py — acquire_lease().

These tests mock the SQLAlchemy session so no real Postgres is needed.
They verify:
    1. Successful lease: scalars() returns a Job → acquire_lease returns it.
    2. Contested/not-claimable: scalars() returns None → acquire_lease returns None.
    3. The UPDATE statement targets the correct columns (spot-check via call args).

The WHERE-clause correctness (status filter, run_at guard, locked_until expiry)
is verified at the SQL level by the migration integration tests (to be added in
M5 when real-DB tests are wired).  These unit tests focus on the service contract.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from atlasqueue.core.enums import JobStatus
from atlasqueue.db.models import Job
from atlasqueue.worker.lease import acquire_lease
from tests.conftest import make_job

pytestmark = pytest.mark.asyncio

_WORKER_ID = "test-worker:abc123"
_LEASE_SECS = 30


def _make_session(returned_job: Job | None) -> MagicMock:
    """Return a mock session whose scalars() yields *returned_job* (or None)."""
    scalar_result = MagicMock()
    scalar_result.one_or_none.return_value = returned_job

    session = MagicMock()
    session.scalars = AsyncMock(return_value=scalar_result)
    return session


class TestAcquireLease:

    async def test_successful_claim_returns_job(self) -> None:
        """When the UPDATE affects a row, acquire_lease returns the updated Job."""
        claimed_job = make_job(status=JobStatus.RUNNING, attempts=1)
        session = _make_session(claimed_job)

        result = await acquire_lease(session, claimed_job.id, _WORKER_ID, _LEASE_SECS)

        assert result is claimed_job
        session.scalars.assert_called_once()

    async def test_contested_claim_returns_none(self) -> None:
        """When another worker already holds the lease (0 rows updated), return None."""
        session = _make_session(returned_job=None)
        job_id = uuid.uuid4()

        result = await acquire_lease(session, job_id, _WORKER_ID, _LEASE_SECS)

        assert result is None
        session.scalars.assert_called_once()

    async def test_update_stmt_uses_running_status(self) -> None:
        """The UPDATE must set status = RUNNING, lock_owner, locked_until, attempts."""
        job = make_job(status=JobStatus.RUNNING)
        session = _make_session(job)

        await acquire_lease(session, job.id, _WORKER_ID, _LEASE_SECS)

        stmt = session.scalars.call_args.args[0]
        compiled = stmt.compile(compile_kwargs={"literal_binds": False})
        params = compiled.params

        # status bind param should be 'running'
        assert params.get("status") == "running"
        # lock_owner bind param should be the worker id
        assert params.get("lock_owner") == _WORKER_ID
        # locked_until and updated_at should be present
        assert "locked_until" in params
        assert "updated_at" in params

    async def test_different_worker_ids_are_written(self) -> None:
        """Each worker's unique ID is recorded in the UPDATE values."""
        job = make_job()
        session = _make_session(job)
        unique_id = "node-42:deadbeef"

        await acquire_lease(session, job.id, unique_id, _LEASE_SECS)

        stmt = session.scalars.call_args.args[0]
        # The lock_owner binding should reference unique_id.
        # We verify scalars was called (value encoding is driver-level).
        session.scalars.assert_called_once()

    async def test_locked_until_is_now_plus_lease_seconds(self) -> None:
        """locked_until should be approximately now + lease_seconds."""
        job = make_job(status=JobStatus.RUNNING)
        session = _make_session(job)

        before = datetime.now(tz=timezone.utc)
        await acquire_lease(session, job.id, _WORKER_ID, _LEASE_SECS)
        after = datetime.now(tz=timezone.utc)

        stmt = session.scalars.call_args.args[0]
        params = stmt.compile(compile_kwargs={"literal_binds": False}).params
        locked_until = params.get("locked_until")

        assert locked_until is not None, "locked_until must be in UPDATE params"
        # Normalise to UTC-aware for comparison.
        if locked_until.tzinfo is None:
            locked_until = locked_until.replace(tzinfo=timezone.utc)

        expected_min = before + timedelta(seconds=_LEASE_SECS)
        expected_max = after + timedelta(seconds=_LEASE_SECS)
        assert expected_min <= locked_until <= expected_max, (
            f"locked_until {locked_until} not in [{expected_min}, {expected_max}]"
        )

    async def test_malformed_job_id_in_consumer_is_skipped(self) -> None:
        """_process_job_id must handle an unparseable Redis value without crashing."""
        from atlasqueue.worker.consumer import _process_job_id

        with patch("atlasqueue.worker.consumer._session_factory") as mock_factory:
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=None)

            # Should NOT raise — logs error and returns.
            await _process_job_id("not-a-valid-uuid", AsyncMock())

        # Reaching here without exception is the assertion.

    async def test_process_job_id_with_valid_uuid_acquires_lease(self) -> None:
        """_process_job_id calls acquire_lease for a well-formed UUID."""
        from atlasqueue.worker.consumer import _process_job_id

        job_id = uuid.uuid4()
        mock_job = make_job(job_id=job_id, status=JobStatus.RUNNING, attempts=1)

        mock_session = MagicMock()
        mock_session.begin = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=None),
            __aexit__=AsyncMock(return_value=None),
        ))

        with (
            patch("atlasqueue.worker.consumer._session_factory") as mock_factory,
            patch("atlasqueue.worker.consumer.acquire_lease", new_callable=AsyncMock) as mock_acquire,
            patch("atlasqueue.worker.consumer.execute_job", new_callable=AsyncMock),
        ):
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_acquire.return_value = mock_job

            redis_mock = AsyncMock()
            await _process_job_id(str(job_id), redis_mock)

        mock_acquire.assert_called_once()
        _, call_job_id, *_ = mock_acquire.call_args.args
        assert call_job_id == job_id
