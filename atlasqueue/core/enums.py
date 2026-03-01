"""Job status enum — shared by ORM, API, and worker."""
from __future__ import annotations

import enum


class JobStatus(str, enum.Enum):
    """Lifecycle states of a Job.

    Allowed transitions (enforced at application layer):
        queued   → running
        running  → succeeded
        running  → failed
        failed   → queued   (retry)
        failed   → dead
    """

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD = "dead"
