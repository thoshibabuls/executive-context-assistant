"""Alembic environment: async migrations over psycopg 3 using the migration (owner) role.

Migrations run only as a release step (BACKEND_DESIGN.md §17.4), never on application startup.
The URL comes from ``API_MIGRATION_DATABASE_URL`` (or ``-x db_url=...`` for tests); the runtime
role that receives DML grants comes from ``API_DB_RUNTIME_ROLE`` (or ``-x runtime_role=...``).
"""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from eca.platform.config import Settings
from eca.platform.db import to_psycopg_url
from eca.platform.runtime import ensure_selector_event_loop_policy

config = context.config
target_metadata = None  # schema is defined by explicit migrations


def _options() -> tuple[str, str]:
    x = context.get_x_argument(as_dictionary=True)
    settings = Settings()
    url = x.get("db_url") or (
        settings.api_migration_database_url.get_secret_value() if settings.api_migration_database_url else ""
    )
    if not url:
        raise RuntimeError("Set API_MIGRATION_DATABASE_URL (or pass -x db_url=...) to run migrations")
    role = x.get("runtime_role") or settings.api_db_runtime_role
    return to_psycopg_url(url), role


def _run(connection: Connection, runtime_role: str) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, transaction_per_migration=True)
    context.config.attributes["runtime_role"] = runtime_role
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    url, role = _options()
    engine = create_async_engine(url, poolclass=pool.NullPool)
    try:
        async with engine.connect() as conn:
            await conn.run_sync(_run, role)
            await conn.commit()
    finally:
        await engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("Offline migrations are not supported; run against a database")

ensure_selector_event_loop_policy()
asyncio.run(_run_async())
