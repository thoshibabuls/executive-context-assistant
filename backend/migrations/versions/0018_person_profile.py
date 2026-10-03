"""Slice 3.4: relationship profile columns on ``persons`` (``people`` module).

BACKEND_DESIGN.md §17.6 (DDL); CONTEXT_ARCHITECTURE.md §5.3 (profile parameters), §11.1-§11.2
(user fields win). ``user_fields`` lists the fields the user set (authority 5), so computed and
header-derived updates leave them alone; ``profile_computed_at`` dates the computed profile in
``interaction_stats``. ``relationship_type`` (0006) is user-set only and gets its value check.
The table keeps its ``persons_user_isolation`` policy; no grant changes.

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-03
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import context, op
from eca.platform.rls import validate_identifier
from eca.platform.roles import check_runtime_roles

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RELATIONSHIP_TYPES = (
    "executive",
    "client",
    "investor",
    "manager",
    "report",
    "partner",
    "stakeholder",
    "colleague",
    "vendor",
    "other",
    "low_priority",
)


def _roles() -> tuple[str, str]:
    api = context.config.attributes.get("runtime_role", "eca_app")
    worker = context.config.attributes.get("worker_role", "eca_worker")
    assert isinstance(api, str) and isinstance(worker, str)
    return validate_identifier(api), validate_identifier(worker)


def upgrade() -> None:
    api, worker = _roles()
    check_runtime_roles(op.get_bind(), api_role=api, worker_role=worker)
    op.execute(
        "ALTER TABLE persons "
        "ADD COLUMN user_fields text[] NOT NULL DEFAULT '{}', "
        "ADD COLUMN profile_computed_at timestamptz NULL"
    )
    values = ", ".join(f"'{v}'" for v in RELATIONSHIP_TYPES)
    op.execute(
        f"ALTER TABLE persons ADD CONSTRAINT ck_persons_relationship_type CHECK (relationship_type IN ({values}))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE persons DROP CONSTRAINT ck_persons_relationship_type")
    op.execute("ALTER TABLE persons DROP COLUMN profile_computed_at, DROP COLUMN user_fields")
