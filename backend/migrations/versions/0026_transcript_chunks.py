"""Slice 4.2: transcript window bounds on ``chunks`` (``retrieval`` module).

CONTEXT_ARCHITECTURE.md §9.9 and §9.11: transcript chunks are speaker-turn windows; their start and
end offsets (milliseconds from the recording start) let citations point at a moment of the
meeting. NULL for every other chunk kind. The table keeps its policy (§7.6).

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-03
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE chunks ADD COLUMN start_ms int NULL, ADD COLUMN end_ms int NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE chunks DROP COLUMN start_ms, DROP COLUMN end_ms")
