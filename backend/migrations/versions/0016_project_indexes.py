"""Slice 2.5: indexes on existing tables for project context, built ``CONCURRENTLY``.

``work_items`` and ``decisions`` already hold the user's history, so their new indexes are built
without blocking writes (BACKEND_DESIGN.md §17.4): ``ix_wi_project`` (items assigned to a
project) and ``ix_decisions_hint_trgm`` (decision hint matching, like ``ix_wi_hint_trgm``).
``CREATE INDEX CONCURRENTLY`` cannot run in a transaction, so this revision runs in Alembic's
autocommit block; ``IF NOT EXISTS`` makes a retry after a failed build safe (an invalid index
left by a failed concurrent build must be dropped by the operator first).

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-02
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_wi_project ON work_items (user_id, project_id) "
            "WHERE project_id IS NOT NULL AND deleted_at IS NULL"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_decisions_hint_trgm ON decisions "
            "USING gin (project_hint gin_trgm_ops) WHERE project_hint IS NOT NULL"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_decisions_hint_trgm")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_wi_project")
