"""Runtime role access: role checks, worker baseline grants, ``alembic_version`` read-only.

BACKEND_DESIGN.md §7.6 and §7.7. Creates no tables and no roles. The API and worker roles must
already exist (provisioned outside migrations); the revision fails loudly otherwise.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-02
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import context, op
from eca.platform.rls import validate_identifier
from eca.platform.roles import check_runtime_roles

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _roles() -> tuple[str, str]:
    api = context.config.attributes.get("runtime_role", "eca_app")
    worker = context.config.attributes.get("worker_role", "eca_worker")
    assert isinstance(api, str) and isinstance(worker, str)
    return validate_identifier(api), validate_identifier(worker)


def _baseline(role: str) -> list[str]:
    # Idempotent: repeats what 0001 granted to the API role (0001 skipped grants when the API role
    # did not exist yet) and gives the worker role the same baseline.
    return [
        f"GRANT USAGE ON SCHEMA public TO {role}",
        f"GRANT EXECUTE ON FUNCTION eca_current_user_id() TO {role}",
        f"GRANT SELECT ON alembic_version TO {role}",
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {role}",
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {role}",
    ]


def upgrade() -> None:
    api, worker = _roles()
    check_runtime_roles(op.get_bind(), api_role=api, worker_role=worker)
    for role in (api, worker):
        for stmt in _baseline(role):
            op.execute(stmt)
    # 0001's GRANT ... ON ALL TABLES ran after Alembic created alembic_version, so the API role
    # also received INSERT, UPDATE and DELETE on it. Readiness needs SELECT only.
    op.execute(f"REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON alembic_version FROM {api}")


def downgrade() -> None:
    _, worker = _roles()
    # The alembic_version correction and the API baseline (owned by 0001) are not reverted.
    op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE USAGE, SELECT ON SEQUENCES FROM {worker}")
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {worker}"
    )
    op.execute(f"REVOKE SELECT ON alembic_version FROM {worker}")
    op.execute(f"REVOKE EXECUTE ON FUNCTION eca_current_user_id() FROM {worker}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {worker}")
