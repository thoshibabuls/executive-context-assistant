"""Slice 3.1: ``push_subscriptions`` (``attention`` module, Web Push).

BACKEND_DESIGN.md §17.6 (DDL), §16.8 (routes); TECHNICAL_DESIGN.md §15.4 (payload-less pushes:
only the endpoint is stored, never the browser's encryption keys). Business table under per-user
RLS with the default DML grants (§7.6). One row per (user, endpoint).

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-03
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import context, op
from eca.platform.rls import drop_user_isolation_ddl, user_isolation_ddl, validate_identifier
from eca.platform.roles import check_runtime_roles

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("push_subscriptions",)


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
        CREATE TABLE push_subscriptions (
          id              uuid PRIMARY KEY,
          user_id         uuid NOT NULL CONSTRAINT fk_push_subscriptions_user REFERENCES users (id),
          endpoint        text NOT NULL,
          expires_at      timestamptz NULL,
          user_agent      text NULL,
          last_success_at timestamptz NULL,
          failure_count   smallint NOT NULL DEFAULT 0,
          revoked_at      timestamptz NULL,
          created_at      timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT ux_push_subscriptions_endpoint UNIQUE (user_id, endpoint)
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
