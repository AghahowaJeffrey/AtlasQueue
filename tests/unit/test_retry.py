"""Unit tests for worker/retry.py — compute_backoff and sweep_failed_jobs."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from atlasqueue.core.config import Settings
from atlasqueue.worker.retry import compute_backoff

pytestmark = pytest.mark.asyncio

# Minimal settings instance for testing — avoids env file loading
_SETTINGS = Settings(
    retry_base_delay_seconds=5,
    retry_jitter_max_seconds=2,
    job_queue_key="atlasqueue:test",
    dlq_key="atlasqueue:test:dlq",
)


class TestComputeBackoff:
    """PRD §9: delay = base_delay * 2^attempts + jitter(0, jitter_max)"""

    def test_first_attempt_delay_is_roughly_10s(self) -> None:
        """After attempt 1: 5 * 2^1 = 10 seconds base."""
        before = datetime.now(tz=timezone.utc)
        run_at = compute_backoff(1, _SETTINGS)
        after = datetime.now(tz=timezone.utc)

        min_expected = before + timedelta(seconds=10)  # 5 * 2^1, jitter min = 0
        max_expected = after + timedelta(seconds=12)   # + jitter_max = 2

        assert min_expected <= run_at <= max_expected

    def test_second_attempt_delay_is_roughly_20s(self) -> None:
        """After attempt 2: 5 * 2^2 = 20 seconds base."""
        before = datetime.now(tz=timezone.utc)
        run_at = compute_backoff(2, _SETTINGS)
        after = datetime.now(tz=timezone.utc)

        min_expected = before + timedelta(seconds=20)
        max_expected = after + timedelta(seconds=22)

        assert min_expected <= run_at <= max_expected

    def test_delay_grows_exponentially(self) -> None:
        """Each additional attempt doubles the base delay."""
        before = datetime.now(tz=timezone.utc)
        run_at_3 = compute_backoff(3, _SETTINGS)
        run_at_4 = compute_backoff(4, _SETTINGS)

        # 5*(2^4) = 80 > 5*(2^3) = 40
        assert run_at_4 > run_at_3

    def test_jitter_is_non_negative(self) -> None:
        """run_at must always be in the future (jitter >= 0)."""
        now = datetime.now(tz=timezone.utc)
        for attempt in range(1, 6):
            run_at = compute_backoff(attempt, _SETTINGS)
            assert run_at > now, f"run_at in the past for attempt {attempt}"

    def test_zero_jitter_max_produces_deterministic_delay(self) -> None:
        """With jitter_max=0, the delay should equal base_delay * 2^attempts exactly."""
        settings = Settings(
            retry_base_delay_seconds=5,
            retry_jitter_max_seconds=0,
        )
        before = datetime.now(tz=timezone.utc)
        run_at = compute_backoff(2, settings)  # 5 * 4 = 20s
        after = datetime.now(tz=timezone.utc)

        # Allow 50ms tolerance for execution time.
        assert (
            before + timedelta(seconds=20, milliseconds=-50)
            <= run_at
            <= after + timedelta(seconds=20, milliseconds=50)
        )


class TestSweepFailedJobs:
    """sweep_failed_jobs() re-enqueues failed jobs and returns count."""

    async def test_no_due_jobs_returns_zero(self) -> None:
        from atlasqueue.worker.retry import sweep_failed_jobs

        # scalars().all() returns empty list
        exe_result = MagicMock()
        exe_result.scalars.return_value.all.return_value = []

        session = MagicMock()
        begin_ctx = AsyncMock()
        begin_ctx.__aenter__ = AsyncMock(return_value=None)
        begin_ctx.__aexit__ = AsyncMock(return_value=None)
        session.begin = MagicMock(return_value=begin_ctx)
        session.execute = AsyncMock(return_value=exe_result)

        redis = AsyncMock()
        count = await sweep_failed_jobs(session, redis, _SETTINGS)

        assert count == 0
        redis.lpush.assert_not_called()

    async def test_due_jobs_are_lpushed_and_returned(self) -> None:
        from atlasqueue.worker.retry import sweep_failed_jobs

        due_ids = [uuid.uuid4(), uuid.uuid4()]
        exe_result = MagicMock()
        exe_result.scalars.return_value.all.return_value = due_ids

        session = MagicMock()
        begin_ctx = AsyncMock()
        begin_ctx.__aenter__ = AsyncMock(return_value=None)
        begin_ctx.__aexit__ = AsyncMock(return_value=None)
        session.begin = MagicMock(return_value=begin_ctx)
        # First execute() call: SELECT (returns ids). Second: UPDATE.
        session.execute = AsyncMock(side_effect=[exe_result, MagicMock()])

        redis = AsyncMock()
        redis.lpush = AsyncMock(return_value=1)

        count = await sweep_failed_jobs(session, redis, _SETTINGS)

        assert count == 2
        assert redis.lpush.call_count == 2
        pushed_ids = {call.args[1] for call in redis.lpush.call_args_list}
        assert pushed_ids == {str(jid) for jid in due_ids}
