"""Readiness checks (no HTTP concepts; ``eca.api`` exposes them)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import structlog
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
READINESS_TIMEOUT_S = 3.0

log = structlog.get_logger("eca.platform.health")


@dataclass(frozen=True)
class ReadinessReport:
    database: bool
    schema_at_head: bool
    current_revision: str | None
    head_revision: str | None

    @property
    def ready(self) -> bool:
        return self.database and self.schema_at_head


def migration_head() -> str | None:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    return ScriptDirectory.from_config(config).get_current_head()


async def check_readiness(
    engine: AsyncEngine | None, *, head: str | None, timeout_s: float = READINESS_TIMEOUT_S
) -> ReadinessReport:
    """Bounded check: probes must answer quickly even when the database is unreachable."""
    if engine is None:
        return ReadinessReport(False, False, None, head)
    try:
        async with asyncio.timeout(timeout_s), engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
            exists = (await conn.execute(text("SELECT to_regclass('public.alembic_version')"))).scalar()
            current = None
            if exists is not None:
                current = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
    except Exception as exc:  # any connection or query failure means "not ready"
        # Log the failure class only: messages can contain connection details.
        log.warning("readiness_check_failed", error_type=type(exc).__name__)
        return ReadinessReport(False, False, None, head)
    return ReadinessReport(True, head is not None and current == head, current, head)
