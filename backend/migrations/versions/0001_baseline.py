"""Baseline: extensions, RLS helper function, runtime-role grants.

No domain tables are created here; each later migration that adds a user-owned table calls
``eca.platform.rls.user_isolation_ddl`` (BACKEND_DESIGN.md §17.1).

Revision ID: 0001
Revises:
Create Date: 2026-10-02
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import context, op
from eca.platform.rls import (
    CREATE_CURRENT_USER_ID_FUNCTION_SQL,
    DROP_CURRENT_USER_ID_FUNCTION_SQL,
    revoke_runtime_role_grants_ddl,
    runtime_role_grants_ddl,
)
from sqlalchemy import text

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MIN_PGVECTOR = (0, 8)


def _runtime_role() -> str:
    role = context.config.attributes.get("runtime_role", "eca_app")
    assert isinstance(role, str)
    return role


def _role_exists(role: str) -> bool:
    return bool(
        op.get_bind().execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}).scalar()
    )


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")

    version = (
        op.get_bind().execute(text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")).scalar()
    )
    parts = tuple(int(p) for p in str(version).split(".")[:2])
    if parts < MIN_PGVECTOR:
        raise RuntimeError(f"pgvector >= 0.8 required (iterative index scans), found {version}")

    op.execute(CREATE_CURRENT_USER_ID_FUNCTION_SQL)
    op.execute("REVOKE ALL ON FUNCTION eca_current_user_id() FROM PUBLIC")

    role = _runtime_role()
    if _role_exists(role):
        attrs = (
            op.get_bind()
            .execute(text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = :r"), {"r": role})
            .one()
        )
        if attrs.rolsuper or attrs.rolbypassrls:
            raise RuntimeError(f"Runtime role {role!r} must not be SUPERUSER or BYPASSRLS")
        for stmt in runtime_role_grants_ddl(role):
            op.execute(stmt)
        # alembic_version predates the default privileges; readiness checks need to read it.
        op.execute(f"GRANT SELECT ON alembic_version TO {role}")


def downgrade() -> None:
    role = _runtime_role()
    if _role_exists(role):
        op.execute(f"REVOKE SELECT ON alembic_version FROM {role}")
        for stmt in revoke_runtime_role_grants_ddl(role):
            op.execute(stmt)
    op.execute(DROP_CURRENT_USER_ID_FUNCTION_SQL)
    op.execute("DROP EXTENSION IF EXISTS citext")
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
    op.execute("DROP EXTENSION IF EXISTS vector")
