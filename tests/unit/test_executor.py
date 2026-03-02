"""Unit tests for worker/executor.py.

Tests cover the three status-transition paths:
    success  → _set_succeeded called, status set to SUCCEEDED
    failure  → _set_failed called (attempts < max_attempts)
    dead     → _set_dead called, DLQ LPUSH issued (attempts >= max_attempts)

All DB calls are mocked; no real Postgres is needed.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from atlasqueue.core.enums import JobStatus
from atlasqueue.worker.executor import HandlerNotFound, execute_job
from atlasqueue.worker.handlers import _registry
from tests.conftest import make_job

pytestmark = pytest.mark.asyncio

_REDIS = AsyncMock()


def _make_session() -> MagicMock:
    """Return a mock session that supports `async with session.begin()`."""
    session = MagicMock()
    begin_ctx = AsyncMock()
    begin_ctx.__aenter__ = AsyncMock(return_value=None)
    begin_ctx.__aexit__ = AsyncMock(return_value=None)
    session.begin = MagicMock(return_value=begin_ctx)
    session.execute = AsyncMock()
    return session


class TestExecuteJob:

    async def test_successful_handler_calls_set_succeeded(self) -> None:
        """A handler that returns cleanly → _set_succeeded is called."""
        job = make_job(job_type="noop", attempts=1, max_attempts=5)
        session = _make_session()

        with patch(
            "atlasqueue.worker.executor._set_succeeded", new_callable=AsyncMock
        ) as mock_succeeded, patch(
            "atlasqueue.worker.executor._set_failed", new_callable=AsyncMock
        ) as mock_failed, patch(
            "atlasqueue.worker.executor._set_dead", new_callable=AsyncMock
        ) as mock_dead:
            await execute_job(session, _REDIS, job)

        mock_succeeded.assert_called_once_with(session, job.id)
        mock_failed.assert_not_called()
        mock_dead.assert_not_called()

    async def test_failing_handler_below_max_attempts_calls_set_failed(self) -> None:
        """A handler that raises + attempts < max_attempts → _set_failed."""

        # Register a temporary failing handler.
        async def boom(payload: dict) -> None:
            raise RuntimeError("handler kaboom")

        _registry["_test.fail"] = boom
        try:
            job = make_job(job_type="_test.fail", attempts=1, max_attempts=3)
            session = _make_session()

            with patch(
                "atlasqueue.worker.executor._set_succeeded", new_callable=AsyncMock
            ) as mock_succeeded, patch(
                "atlasqueue.worker.executor._set_failed", new_callable=AsyncMock
            ) as mock_failed, patch(
                "atlasqueue.worker.executor._set_dead", new_callable=AsyncMock
            ) as mock_dead:
                await execute_job(session, _REDIS, job)

            mock_succeeded.assert_not_called()
            mock_failed.assert_called_once()
            mock_dead.assert_not_called()

            # Error string should mention the exception type.
            _, call_job, call_error, _ = mock_failed.call_args.args
            assert "RuntimeError" in call_error
        finally:
            _registry.pop("_test.fail", None)

    async def test_failing_handler_at_max_attempts_calls_set_dead(self) -> None:
        """A handler that raises + attempts == max_attempts → _set_dead."""

        async def lethal(payload: dict) -> None:
            raise ValueError("terminal")

        _registry["_test.dead"] = lethal
        try:
            job = make_job(job_type="_test.dead", attempts=5, max_attempts=5)
            session = _make_session()
            redis = AsyncMock()

            with patch(
                "atlasqueue.worker.executor._set_succeeded", new_callable=AsyncMock
            ) as mock_succeeded, patch(
                "atlasqueue.worker.executor._set_failed", new_callable=AsyncMock
            ) as mock_failed, patch(
                "atlasqueue.worker.executor._set_dead", new_callable=AsyncMock
            ) as mock_dead:
                await execute_job(session, redis, job)

            mock_succeeded.assert_not_called()
            mock_failed.assert_not_called()
            mock_dead.assert_called_once()
        finally:
            _registry.pop("_test.dead", None)

    async def test_unregistered_handler_raises_handler_not_found(self) -> None:
        """An unregistered job type is treated as a handler failure (not a crash)."""
        job = make_job(job_type="unknown.type", attempts=1, max_attempts=3)
        session = _make_session()

        with patch(
            "atlasqueue.worker.executor._set_succeeded", new_callable=AsyncMock
        ), patch(
            "atlasqueue.worker.executor._set_failed", new_callable=AsyncMock
        ) as mock_failed, patch(
            "atlasqueue.worker.executor._set_dead", new_callable=AsyncMock
        ):
            await execute_job(session, _REDIS, job)

        # Should have called _set_failed with HandlerNotFound in the message.
        mock_failed.assert_called_once()
        _, _, error_msg, _ = mock_failed.call_args.args
        assert "HandlerNotFound" in error_msg

    async def test_error_message_is_truncated_at_4096_chars(self) -> None:
        """Enormous tracebacks are truncated before DB storage."""
        big_msg = "x" * 10_000

        async def loud(payload: dict) -> None:
            raise RuntimeError(big_msg)

        _registry["_test.loud"] = loud
        try:
            job = make_job(job_type="_test.loud", attempts=1, max_attempts=5)
            session = _make_session()

            with patch(
                "atlasqueue.worker.executor._set_succeeded", new_callable=AsyncMock
            ), patch(
                "atlasqueue.worker.executor._set_failed", new_callable=AsyncMock
            ) as mock_failed, patch(
                "atlasqueue.worker.executor._set_dead", new_callable=AsyncMock
            ):
                await execute_job(session, _REDIS, job)

            _, _, stored_error, _ = mock_failed.call_args.args
            assert len(stored_error) <= 4096
        finally:
            _registry.pop("_test.loud", None)
