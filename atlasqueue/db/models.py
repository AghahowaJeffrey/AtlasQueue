"""SQLAlchemy ORM models for AtlasQueue.

Defines:
    - ``Job``            — primary job record (``jobs`` table)
    - ``IdempotencyKey`` — deduplication record (``idempotency_keys`` table)
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone


def _utcnow() -> datetime:
    """Return current UTC datetime (used as Python-side column default)."""
    return datetime.now(tz=timezone.utc)

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from atlasqueue.core.enums import JobStatus


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------

class Job(Base):
    """Durable record of a single unit of async work."""

    __tablename__ = "jobs"

    __table_args__ = (
        # Hot path: worker queries by (status, run_at) to find claimable jobs.
        Index("ix_jobs_status_run_at", "status", "run_at"),
        # Lease expiry sweep: find jobs whose lock has timed out.
        Index("ix_jobs_locked_until", "locked_until"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        insert_default=uuid.uuid4,
    )
    type: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[JobStatus] = mapped_column(
        Enum(
            JobStatus,
            name="job_status",
            values_callable=lambda e: [m.value for m in e],
            create_type=False,  # migration creates the type explicitly
        ),
        nullable=False,
        default=JobStatus.QUEUED,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)

    # Scheduling / lease fields
    run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lock_owner: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Diagnostics
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
        onupdate=_utcnow,
    )

    # Relationships
    idempotency_key: Mapped[IdempotencyKey | None] = relationship(
        "IdempotencyKey", back_populates="job", uselist=False
    )

    def __repr__(self) -> str:
        return f"<Job id={self.id} type={self.type!r} status={self.status.value}>"


# ---------------------------------------------------------------------------
# idempotency_keys
# ---------------------------------------------------------------------------

class IdempotencyKey(Base):
    """Prevents duplicate job submission for the same Idempotency-Key header."""

    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(String(512), primary_key=True)
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    # Relationships
    job: Mapped[Job] = relationship("Job", back_populates="idempotency_key")

    def __repr__(self) -> str:
        return f"<IdempotencyKey key={self.key!r} job_id={self.job_id}>"
