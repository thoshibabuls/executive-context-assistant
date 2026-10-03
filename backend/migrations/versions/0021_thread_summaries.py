"""Slice 3.2: AI-03 thread-summary provenance on ``conversations`` (``communication`` module).

BACKEND_DESIGN.md §17.6 (DDL), §15 (``thread_summary`` keyed by the last covered message);
AI_PIPELINE.md §5.1 (provenance), §5.9 (AI-03). ``summary`` and ``summary_through_message_id``
exist since 0007; this adds the method, model, prompt version, time, AI call IDs and covered
source items, plus the claim (``summary_pending_through``) and failure marker of the debounced
job. The table keeps its ``conversations_user_isolation`` policy; no grant changes.

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-03
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import context, op
from eca.platform.rls import validate_identifier
from eca.platform.roles import check_runtime_roles

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = (
    "summary_key_points",
    "summary_method",
    "summary_model",
    "summary_prompt_version",
    "summary_derived_at",
    "summary_ai_call_ids",
    "summary_covered_source_ids",
    "summary_pending_through",
    "summary_failed_through",
    "summary_requested_at",
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
        """
        ALTER TABLE conversations
          ADD COLUMN summary_key_points jsonb NOT NULL DEFAULT '[]',
          ADD COLUMN summary_method text NULL CONSTRAINT ck_conversations_summary_method CHECK (summary_method IN ('llm')),
          ADD COLUMN summary_model text NULL,
          ADD COLUMN summary_prompt_version text NULL,
          ADD COLUMN summary_derived_at timestamptz NULL,
          ADD COLUMN summary_ai_call_ids uuid[] NOT NULL DEFAULT '{}',
          ADD COLUMN summary_covered_source_ids uuid[] NOT NULL DEFAULT '{}',
          ADD COLUMN summary_pending_through uuid NULL,
          ADD COLUMN summary_failed_through uuid NULL,
          ADD COLUMN summary_requested_at timestamptz NULL
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE conversations " + ", ".join(f"DROP COLUMN {c}" for c in COLUMNS))
