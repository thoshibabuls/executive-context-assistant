"""Slice 2.3: ``user_checkpoints`` owned by ``identity`` ("what changed since I last looked").

CONTEXT_ARCHITECTURE.md §5.2 (A1) and §7.1; BACKEND_DESIGN.md §17.5 (DDL), §6.2 (the later
timestamp wins). A user-owned business table under ``user_checkpoints_user_isolation``.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-02
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import context, op
from eca.platform.rls import drop_user_isolation_ddl, user_isolation_ddl, validate_identifier
from eca.platform.roles import check_runtime_roles

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _roles() -> tuple[str, str]:
    api = context.config.attributes.get("runtime_role", "eca_app")
    worker = context.config.attributes.get("worker_role", "eca_worker")
    assert isinstance(api, str) and isinstance(worker, str)
    return validate_identifier(api), validate_identifier(worker)


def upgrade() -> None:
    api, worker = _roles()
    check_runtime_roles(op.get_bind(), api_role=api, worker_role=worker)

    op.execute(
        r"""
        CREATE TABLE user_checkpoints (
          user_id      uuid NOT NULL CONSTRAINT fk_user_checkpoints_user REFERENCES users (id),
          surface      text NOT NULL CONSTRAINT ck_user_checkpoints_surface
                       CHECK (surface ~ '^(today|chat|(person|project|meeting):[0-9a-f-]{36})$'),
          last_seen_at timestamptz NOT NULL,
          updated_at   timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT pk_user_checkpoints PRIMARY KEY (user_id, surface)
        )
        """
    )
    for stmt in user_isolation_ddl("user_checkpoints"):
        op.execute(stmt)


def downgrade() -> None:
    for stmt in drop_user_isolation_ddl("user_checkpoints"):
        op.execute(stmt)
    op.execute("DROP TABLE user_checkpoints")
