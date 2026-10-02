"""Procrastinate 3.10.0 schema, applied from a frozen vendored copy, with role grants.

BACKEND_DESIGN.md §7.7 and §15. The SQL file is never read from the installed package, so this
revision does not change when the package changes; a version bump needs a new revision (a test
compares the migrated schema with the installed package's ``schema.sql``).

Privileges (BACKEND_DESIGN.md §7.6): the API role has no access to any Procrastinate object; the
worker role keeps the default table and sequence privileges and gets EXECUTE on every
``procrastinate_*`` function, which is revoked from PUBLIC (per function, never schema-wide,
because extension functions must stay callable).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-02
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from alembic import context, op
from eca.platform.rls import validate_identifier
from eca.platform.roles import check_runtime_roles
from sqlalchemy import text

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROCRASTINATE_VERSION = "3.10.0"
SCHEMA_FILE = Path(__file__).resolve().parents[1] / "sql" / f"procrastinate_{PROCRASTINATE_VERSION}_schema.sql"

_TABLES = ("procrastinate_events", "procrastinate_periodic_defers", "procrastinate_jobs", "procrastinate_workers")
_TYPES = ("procrastinate_job_to_defer_v1", "procrastinate_job_event_type", "procrastinate_job_status")

_RELATIONS_SQL = text(
    """
    SELECT c.relname, c.relkind
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public' AND c.relname LIKE 'procrastinate\\_%' AND c.relkind IN ('r', 'S')
     ORDER BY c.relname
    """
)
_FUNCTIONS_SQL = text(
    """
    SELECT p.oid::regprocedure::text
      FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'public' AND p.proname LIKE 'procrastinate\\_%'
       AND (CAST(:triggers AS boolean) IS NULL OR (p.prorettype = 'trigger'::regtype) = :triggers)
     ORDER BY 1
    """
)


def _roles() -> tuple[str, str]:
    api = context.config.attributes.get("runtime_role", "eca_app")
    worker = context.config.attributes.get("worker_role", "eca_worker")
    assert isinstance(api, str) and isinstance(worker, str)
    return validate_identifier(api), validate_identifier(worker)


def upgrade() -> None:
    api, worker = _roles()
    bind = op.get_bind()
    check_runtime_roles(bind, api_role=api, worker_role=worker)

    # The script has several statements and '%' in PL/pgSQL messages: send it without parameters.
    bind.execution_options(no_parameters=True).exec_driver_sql(SCHEMA_FILE.read_text(encoding="utf-8"))

    for name, kind in bind.execute(_RELATIONS_SQL).all():
        if kind == "r":
            op.execute(f"REVOKE ALL ON TABLE {name} FROM {api}")
            op.execute(f"REVOKE TRUNCATE, REFERENCES, TRIGGER ON TABLE {name} FROM {worker}")
        else:
            op.execute(f"REVOKE ALL ON SEQUENCE {name} FROM {api}")
            op.execute(f"REVOKE UPDATE ON SEQUENCE {name} FROM {worker}")
    for signature in bind.execute(_FUNCTIONS_SQL, {"triggers": None}).scalars().all():
        op.execute(f"REVOKE EXECUTE ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {worker}")


def downgrade() -> None:
    bind = op.get_bind()
    # Order: functions returning table row types, then tables (with their triggers, indexes and
    # grants), then the trigger functions, then the types. No CASCADE.
    for signature in bind.execute(_FUNCTIONS_SQL, {"triggers": False}).scalars().all():
        op.execute(f"DROP FUNCTION {signature}")
    for table in _TABLES:
        op.execute(f"DROP TABLE {table}")
    for signature in bind.execute(_FUNCTIONS_SQL, {"triggers": True}).scalars().all():
        op.execute(f"DROP FUNCTION {signature}")
    for type_name in _TYPES:
        op.execute(f"DROP TYPE {type_name}")
