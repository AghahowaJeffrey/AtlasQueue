"""0001_initial — create job_status enum, jobs table, idempotency_keys table.

Revision ID: 0001
Revises: (none — initial migration)
Create Date: 2026-03-01
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers
revision: str = "0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Native Postgres enum for job lifecycle states.
job_status_enum = postgresql.ENUM(
    "queued",
    "running",
    "succeeded",
    "failed",
    "dead",
    name="job_status",
)


def upgrade() -> None:
    # 1. Create the native PG enum type first (cannot be created inline on
    #    some Postgres versions when referenced from multiple columns).
    job_status_enum.create(op.get_bind(), checkfirst=True)

    # 2. jobs
    op.create_table(
        "jobs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("type", sa.String(255), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "running",
                "succeeded",
                "failed",
                "dead",
                name="job_status",
                create_type=False,
            ),
            nullable=False,
            server_default="queued",
        ),
        sa.Column(
            "attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "max_attempts",
            sa.Integer(),
            nullable=False,
            server_default="5",
        ),
        sa.Column(
            "run_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lock_owner", sa.String(255), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # Indexes from PRD §6.
    op.create_index("ix_jobs_status_run_at", "jobs", ["status", "run_at"])
    op.create_index("ix_jobs_locked_until", "jobs", ["locked_until"])

    # 3. idempotency_keys
    op.create_table(
        "idempotency_keys",
        sa.Column("key", sa.String(512), primary_key=True, nullable=False),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("idempotency_keys")
    op.drop_index("ix_jobs_locked_until", table_name="jobs")
    op.drop_index("ix_jobs_status_run_at", table_name="jobs")
    op.drop_table("jobs")
    job_status_enum.drop(op.get_bind(), checkfirst=True)
