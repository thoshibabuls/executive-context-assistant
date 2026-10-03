"""Slice 4.2: ``transcript_segments`` (``meetings`` module).

BACKEND_DESIGN.md §17.7. Versioned by ``(recording_id, transcription_model)``: a new version is
written in one transaction (delete that key's segments, insert the new ones) and other versions
stay for their evidence; re-transcription is never automatic (AI_PIPELINE.md §15). Timestamps are
NULL for transcript files without timing. SOURCE data (machine transcription of user media),
business table under per-user RLS with the default DML grants (§7.6).

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-03
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import context, op
from eca.platform.rls import drop_user_isolation_ddl, user_isolation_ddl, validate_identifier
from eca.platform.roles import check_runtime_roles

revision: str = "0025"
down_revision: str | None = "0024"
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
        """
        CREATE TABLE transcript_segments (
          id                  uuid PRIMARY KEY,
          user_id             uuid NOT NULL CONSTRAINT fk_transcript_segments_user REFERENCES users (id),
          recording_id        uuid NOT NULL CONSTRAINT fk_transcript_segments_recording REFERENCES recordings (id),
          transcription_model text NOT NULL,
          seq                 int NOT NULL,
          start_ms            int NULL,
          end_ms              int NULL,
          speaker_label       text NULL,
          text                text NOT NULL,
          created_at          timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT ux_transcript_segments UNIQUE (recording_id, transcription_model, seq)
        )
        """
    )
    for stmt in user_isolation_ddl("transcript_segments"):
        op.execute(stmt)


def downgrade() -> None:
    for stmt in drop_user_isolation_ddl("transcript_segments"):
        op.execute(stmt)
    op.execute("DROP TABLE transcript_segments")
