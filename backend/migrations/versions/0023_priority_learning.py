"""Slice 3.5: priority learning tables (``attention`` module): ``priority_pairs`` (preference pairs
recorded when the user overrides a priority) and ``user_priority_weights`` (fitted per-user
multipliers, bounded to [0.5, 2]).

BACKEND_DESIGN.md §17.6 (DDL); TECHNICAL_DESIGN.md §12.6-§12.8 (fitting; priority stays a
deterministic, explainable formula). Feature snapshots are numbers only, no content. Business
tables under per-user RLS with the default DML grants (§7.6).

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-03
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import context, op
from eca.platform.rls import drop_user_isolation_ddl, user_isolation_ddl, validate_identifier
from eca.platform.roles import check_runtime_roles

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("priority_pairs", "user_priority_weights")


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
        CREATE TABLE priority_pairs (
          id                 uuid PRIMARY KEY,
          user_id            uuid NOT NULL CONSTRAINT fk_priority_pairs_user REFERENCES users (id),
          preferred_type     text NOT NULL CONSTRAINT ck_priority_pairs_preferred_type
                             CHECK (preferred_type IN ('work_item', 'conversation')),
          preferred_id       uuid NOT NULL,
          other_type         text NOT NULL CONSTRAINT ck_priority_pairs_other_type
                             CHECK (other_type IN ('work_item', 'conversation')),
          other_id           uuid NOT NULL,
          preferred_features jsonb NOT NULL,
          other_features     jsonb NOT NULL,
          source             text NOT NULL CONSTRAINT ck_priority_pairs_source
                             CHECK (source IN ('override_up', 'override_down')),
          config_version     text NOT NULL,
          created_at         timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT ux_priority_pairs UNIQUE (user_id, preferred_type, preferred_id, other_type, other_id)
        )
        """
    )
    op.execute("CREATE INDEX ix_priority_pairs_user ON priority_pairs (user_id, created_at DESC)")
    op.execute(
        """
        CREATE TABLE user_priority_weights (
          user_id        uuid PRIMARY KEY CONSTRAINT fk_user_priority_weights_user REFERENCES users (id),
          multipliers    jsonb NOT NULL,
          pairs_used     int NOT NULL,
          agreement      real NOT NULL,
          config_version text NOT NULL,
          fitted_at      timestamptz NOT NULL
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
