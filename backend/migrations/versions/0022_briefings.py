"""Slice 3.2: ``briefings`` (``attention`` module): one deterministic daily briefing per user-day.

BACKEND_DESIGN.md §17.6 (DDL), §15 (``daily_briefing``, lock ``brief:{user}:{date}``), §18 (one per
day; the "updated since briefing" banner is computed at read time). No ``ai_call_id``: AI-12 is
retired and the briefing makes no model call. Business table under per-user RLS with the default
DML grants (§7.6).

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-03
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import context, op
from eca.platform.rls import drop_user_isolation_ddl, user_isolation_ddl, validate_identifier
from eca.platform.roles import check_runtime_roles

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("briefings",)


def _roles() -> tuple[str, str]:
    api = context.config.attributes.get("runtime_role", "eca_app")
    worker = context.config.attributes.get("worker_role", "eca_worker")
    assert isinstance(api, str) and isinstance(worker, str)
    return validate_identifier(api), validate_identifier(worker)


def upgrade() -> None:
    api, worker = _roles()
    check_runtime_roles(op.get_bind(), api_role=api, worker_role=worker)
    op.execute(
        """
        CREATE TABLE briefings (
          user_id      uuid NOT NULL CONSTRAINT fk_briefings_user REFERENCES users (id),
          date         date NOT NULL,
          timezone     text NOT NULL,
          headline     text NOT NULL,
          content      jsonb NOT NULL,
          generated_at timestamptz NOT NULL,
          trigger      text NOT NULL CONSTRAINT ck_briefings_trigger CHECK (trigger IN ('schedule', 'on_demand')),
          CONSTRAINT pk_briefings PRIMARY KEY (user_id, date)
        )
        """
    )
    for table in TABLES:
        for stmt in user_isolation_ddl(table):
            op.execute(stmt)


def downgrade() -> None:
    for table in reversed(TABLES):
        for stmt in drop_user_isolation_ddl(table):
            op.execute(stmt)
        op.execute(f"DROP TABLE {table}")
