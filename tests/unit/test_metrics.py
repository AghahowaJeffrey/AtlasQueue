"""Unit tests for observability — metrics registration and /metrics endpoint.

Tests verify:
    1. All expected metric names appear in the Prometheus text output.
    2. GET /metrics returns 200 with prometheus text-format content.
    3. JOBS_SUBMITTED counter increments correctly.
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient
from prometheus_client import generate_latest

# Ensure all metrics are registered by importing the module
import atlasqueue.core.metrics as _m  # noqa: F401

pytestmark = pytest.mark.asyncio


def _scrape() -> str:
    """Return the current Prometheus text snapshot as a string."""
    return generate_latest().decode()


class TestMetricsRegistry:
    """All metrics must be present in the Prometheus output after import."""

    def test_job_lifecycle_counters_registered(self) -> None:
        output = _scrape()
        expected = [
            "atlasqueue_jobs_submitted_total",
            "atlasqueue_jobs_succeeded_total",
            "atlasqueue_jobs_failed_total",
            "atlasqueue_jobs_dead_total",
            "atlasqueue_jobs_idempotent_total",
            "atlasqueue_jobs_retried_total",
        ]
        for name in expected:
            assert name in output, f"Missing metric in scrape output: {name}"

    def test_lease_counters_registered(self) -> None:
        output = _scrape()
        assert "atlasqueue_lease_acquired_total" in output
        assert "atlasqueue_lease_rejected_total" in output

    def test_worker_gauges_and_histograms_registered(self) -> None:
        output = _scrape()
        assert "atlasqueue_worker_active_jobs" in output
        assert "atlasqueue_job_duration_seconds" in output
        assert "atlasqueue_http_request_duration_seconds" in output
        assert "atlasqueue_http_requests_total" in output

    def test_jobs_submitted_counter_increments(self) -> None:
        from atlasqueue.core.metrics import JOBS_SUBMITTED

        _label = "_test.metrics.inc"
        # Baseline — may be 0 or higher if tests ran before.
        before_text = _scrape()
        # Count occurrences of our specific label value.
        before = sum(
            float(line.split()[-1])
            for line in before_text.splitlines()
            if "atlasqueue_jobs_submitted_total" in line
            and f'job_type="{_label}"' in line
        )

        JOBS_SUBMITTED.labels(job_type=_label).inc()

        after_text = _scrape()
        after = sum(
            float(line.split()[-1])
            for line in after_text.splitlines()
            if "atlasqueue_jobs_submitted_total" in line
            and f'job_type="{_label}"' in line
        )
        assert after == before + 1.0


class TestMetricsEndpoint:
    """GET /metrics (or /metrics/) must return 200 with Prometheus text format."""

    async def test_metrics_endpoint_returns_200(
        self, client: AsyncClient
    ) -> None:
        # The ASGI-mounted app may redirect /metrics → /metrics/;
        # follow_redirects ensures we land on the actual response.
        resp = await client.get("/metrics/", follow_redirects=True)
        assert resp.status_code == 200

    async def test_metrics_content_type_is_prometheus(
        self, client: AsyncClient
    ) -> None:
        resp = await client.get("/metrics/", follow_redirects=True)
        assert "text/plain" in resp.headers.get("content-type", "")

    async def test_metrics_body_contains_known_metric(
        self, client: AsyncClient
    ) -> None:
        resp = await client.get("/metrics/", follow_redirects=True)
        # Any standard prometheus-client metric is always present.
        assert "python_info" in resp.text or "atlasqueue_" in resp.text
