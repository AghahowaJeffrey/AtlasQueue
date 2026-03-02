"""Prometheus metrics for AtlasQueue.

All metrics are defined here and imported by the modules that instrument them.
This single source-of-truth approach avoids name collisions and makes it easy
to see all exposed metrics in one place.

Metric naming follows Prometheus conventions:
    atlasqueue_<subsystem>_<name>_<unit>

Exposed surfaces
----------------
API process:    /metrics (mounted ASGI endpoint, prometheus_client multiproc)
Worker process: HTTP on WORKER_METRICS_PORT (default 9091) via a background
                thread started in worker/main.py
"""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# ---------------------------------------------------------------------------
# Job lifecycle counters
# ---------------------------------------------------------------------------

JOBS_SUBMITTED = Counter(
    "atlasqueue_jobs_submitted_total",
    "Total jobs submitted via POST /v1/jobs",
    ["job_type"],
)

JOBS_SUCCEEDED = Counter(
    "atlasqueue_jobs_succeeded_total",
    "Total jobs that completed successfully",
    ["job_type"],
)

JOBS_FAILED = Counter(
    "atlasqueue_jobs_failed_total",
    "Total job execution failures (retryable)",
    ["job_type"],
)

JOBS_DEAD = Counter(
    "atlasqueue_jobs_dead_total",
    "Total jobs exhausted all retries and moved to DLQ",
    ["job_type"],
)

JOBS_IDEMPOTENT = Counter(
    "atlasqueue_jobs_idempotent_total",
    "Total duplicate submissions resolved via idempotency key",
    ["job_type"],
)

JOBS_RETRIED = Counter(
    "atlasqueue_jobs_retried_total",
    "Total failed jobs re-enqueued by the sweep loop",
)

# ---------------------------------------------------------------------------
# Lease acquisition counters
# ---------------------------------------------------------------------------

LEASE_ACQUIRED = Counter(
    "atlasqueue_lease_acquired_total",
    "Total successful lease acquisitions",
)

LEASE_REJECTED = Counter(
    "atlasqueue_lease_rejected_total",
    "Total failed lease acquisitions (job not claimable)",
)

# ---------------------------------------------------------------------------
# Worker gauges
# ---------------------------------------------------------------------------

WORKER_ACTIVE_JOBS = Gauge(
    "atlasqueue_worker_active_jobs",
    "Number of jobs currently being executed by this worker process",
)

# ---------------------------------------------------------------------------
# Timing histograms
# ---------------------------------------------------------------------------

JOB_DURATION = Histogram(
    "atlasqueue_job_duration_seconds",
    "Handler execution wall-clock time",
    ["job_type", "outcome"],  # outcome: succeeded | failed | dead
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)

HTTP_REQUEST_DURATION = Histogram(
    "atlasqueue_http_request_duration_seconds",
    "API HTTP request latency",
    ["method", "path_template", "status_code"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5),
)

HTTP_REQUESTS_TOTAL = Counter(
    "atlasqueue_http_requests_total",
    "Total API HTTP requests",
    ["method", "path_template", "status_code"],
)
