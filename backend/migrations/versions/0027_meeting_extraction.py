"""Slice 4.3: meeting extraction (``meetings`` and ``work`` modules).

BACKEND_DESIGN.md §17.7: provenance of the AI-10 meeting summary on ``meetings``; speaker mapping
on ``meeting_participants`` (diarization labels per person with origin, method, confidence and
status: ``applied`` mappings resolve speakers, ``proposed`` ones wait for the user); and
``decisions.meeting_id`` with its index. The tables keep their policies and grants (§7.6). No
cascades: deletion order is explicit (§13.3).

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-03
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE meetings
          ADD COLUMN summary_extraction_id uuid NULL
              CONSTRAINT fk_meetings_summary_extraction REFERENCES extractions (id),
          ADD COLUMN summary_method text NULL CONSTRAINT ck_meetings_summary_method CHECK (summary_method IN ('llm')),
          ADD COLUMN summary_model text NULL,
          ADD COLUMN summary_prompt_version text NULL,
          ADD COLUMN summary_derived_at timestamptz NULL
        """
    )
    op.execute(
        """
        ALTER TABLE meeting_participants
          ADD COLUMN speaker_labels text[] NOT NULL DEFAULT '{}',
          ADD COLUMN mapping_status text NULL
              CONSTRAINT ck_mp_mapping_status CHECK (mapping_status IN ('proposed', 'applied')),
          ADD COLUMN mapping_origin text NULL
              CONSTRAINT ck_mp_mapping_origin CHECK (mapping_origin IN ('deterministic', 'ai', 'user')),
          ADD COLUMN mapping_method text NULL,
          ADD COLUMN mapping_confidence real NULL,
          ADD COLUMN mapping_extraction_id uuid NULL
              CONSTRAINT fk_mp_mapping_extraction REFERENCES extractions (id),
          ADD COLUMN mapping_model text NULL,
          ADD COLUMN mapping_derived_at timestamptz NULL,
          ADD COLUMN mapping_confirmed_at timestamptz NULL
        """
    )
    op.execute(
        "ALTER TABLE decisions ADD COLUMN meeting_id uuid NULL "
        "CONSTRAINT fk_decisions_meeting REFERENCES meetings (id)"
    )
    op.execute("CREATE INDEX ix_decisions_meeting ON decisions (user_id, meeting_id) WHERE meeting_id IS NOT NULL")


def downgrade() -> None:
    op.execute("DROP INDEX ix_decisions_meeting")
    op.execute("ALTER TABLE decisions DROP COLUMN meeting_id")
    op.execute(
        """
        ALTER TABLE meeting_participants
          DROP COLUMN speaker_labels, DROP COLUMN mapping_status, DROP COLUMN mapping_origin,
          DROP COLUMN mapping_method, DROP COLUMN mapping_confidence, DROP COLUMN mapping_extraction_id,
          DROP COLUMN mapping_model, DROP COLUMN mapping_derived_at, DROP COLUMN mapping_confirmed_at
        """
    )
    op.execute(
        """
        ALTER TABLE meetings
          DROP COLUMN summary_extraction_id, DROP COLUMN summary_method, DROP COLUMN summary_model,
          DROP COLUMN summary_prompt_version, DROP COLUMN summary_derived_at
        """
    )
