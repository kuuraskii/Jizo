"""
Alembic environment for JIZO.

The one job here: pull the database URL from `backend/secrets.py` at
runtime rather than reading `alembic.ini`. That keeps a single source of
truth for credentials, so a migrated database and a running app can never
disagree about which database they are talking to.

Migration files use async because our engine is async (asyncpg).
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy import pool

from backend.secrets import get_config
from backend.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Alembic compares against this metadata, so autogenerate sees every table.
target_metadata = Base.metadata


def _database_url() -> str:
    """Resolve the URL from settings, failing loudly if it is missing."""
    url = get_config().database_url
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy .env.example to .env, or export "
            "the variable, then re-run the migration."
        )
    if "+asyncpg" not in url:
        raise RuntimeError(
            f"DATABASE_URL must use the async driver, got {url!r}"
        )
    return url


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting. Used for review/diffing."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    """Run migrations against a live connection."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # `compare_type` compares COLUMN TYPES only. It does NOT compare
        # CHECK constraints - Alembic ignores those during autogenerate. So
        # a change to the PHASE_VALUES / FAULT_VALUES vocabulary in
        # models.py would go undetected; adding a value there requires
        # hand-writing the migration. (An earlier comment here claimed
        # otherwise.)
        compare_type=True,
        # Detects a mismatch between a model's client-side default and the
        # database's server default, so model/migration default drift shows
        # up as a revision instead of silently differing between a
        # `create_all` schema and a migrated one.
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Connect with the async engine and migrate."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _database_url()

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Entry point for online migrations."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()