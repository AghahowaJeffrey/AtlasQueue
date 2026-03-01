"""Alembic async migration environment.

Supports both offline (generate SQL) and online (apply to live DB) modes.
Uses asyncpg driver via run_async_migrations().
"""
from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from atlasqueue.core.config import get_settings
from atlasqueue.db.models import Base

# Alembic Config object — gives access to values in alembic.ini.
config = context.config

# Configure Python logging from alembic.ini [loggers] section.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Make model metadata available for autogenerate.
target_metadata = Base.metadata

settings = get_settings()


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live DB connection (useful for review/CI)."""
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):  # type: ignore[no-untyped-def]
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Apply migrations against a live async connection."""
    async_engine = create_async_engine(settings.database_url)
    async with async_engine.connect() as conn:
        await conn.run_sync(do_run_migrations)
    await async_engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
