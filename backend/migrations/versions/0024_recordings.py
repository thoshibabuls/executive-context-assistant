"""Slice 4.1: ``recordings`` (``meetings`` module): uploaded meeting media and transcript files, and
``meetings.origin`` (``calendar`` or ``upload``: the meeting row an unlinked upload gets, §17.7).

BACKEND_DESIGN.md §17.7 (DDL), §11.4 (upload flow), §10.1 (``UNIQUE (user_id, sha256)``: the same
media uploaded again returns the existing recording). One recording per meeting
(``ux_recordings_meeting``), so speaker labels of two transcripts never mix. Business table under
per-user RLS with the default DML grants (§7.6); no new cross-user policy.

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-03
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import context, op
from eca.platform.rls import drop_user_isolation_ddl, user_isolation_ddl, validate_identifier
from eca.platform.roles import check_runtime_roles

revision: str = "0024"
down_revision: str | None = "0023"
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
        CREATE TABLE recordings (
          id                       uuid PRIMARY KEY,
          user_id                  uuid NOT NULL CONSTRAINT fk_recordings_user REFERENCES users (id),
          kind                     text NOT NULL CONSTRAINT ck_recordings_kind
                                   CHECK (kind IN ('media', 'transcript_file')),
          mime                     text NOT NULL,
          bytes                    bigint NOT NULL CONSTRAINT ck_recordings_bytes CHECK (bytes > 0),
          sha256                   bytea NOT NULL,
          title                    text NULL,
          occurred_at              timestamptz NULL,
          storage_key              text NOT NULL,
          audio_storage_key        text NULL,
          source_item_id           uuid NULL CONSTRAINT fk_recordings_source_item REFERENCES source_items (id),
          meeting_id               uuid NULL CONSTRAINT fk_recordings_meeting REFERENCES meetings (id),
          status                   text NOT NULL DEFAULT 'pending_upload' CONSTRAINT ck_recordings_status
                                   CHECK (status IN ('pending_upload', 'uploaded', 'preparing', 'transcribing',
                                                     'transcribed', 'extracting', 'ready', 'failed', 'rejected')),
          failed_stage             text NULL CONSTRAINT ck_recordings_failed_stage
                                   CHECK (failed_stage IN ('prepare', 'transcribe', 'extract')),
          error_code               text NULL,
          stage_attempts           smallint NOT NULL DEFAULT 0,
          next_attempt_at          timestamptz NULL,
          duration_s               real NULL,
          transcription_model      text NULL,
          transcript_hash          bytea NULL,
          provider_file_ref        jsonb NULL,
          provider_file_expires_at timestamptz NULL,
          upload_expires_at        timestamptz NOT NULL,
          processed_at             timestamptz NULL,
          raw_purged_at            timestamptz NULL,
          version                  int NOT NULL DEFAULT 1,
          created_at               timestamptz NOT NULL DEFAULT now(),
          updated_at               timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE UNIQUE INDEX ux_recordings_sha ON recordings (user_id, sha256)")
    op.execute("CREATE UNIQUE INDEX ux_recordings_meeting ON recordings (meeting_id) WHERE meeting_id IS NOT NULL")
    op.execute("CREATE INDEX ix_recordings_user_time ON recordings (user_id, created_at DESC, id)")
    op.execute("CREATE INDEX ix_recordings_due ON recordings (next_attempt_at) WHERE next_attempt_at IS NOT NULL")
    op.execute(
        "CREATE INDEX ix_recordings_purge ON recordings (updated_at) "
        "WHERE raw_purged_at IS NULL AND status IN ('ready', 'failed', 'rejected')"
    )
    op.execute(
        "CREATE TRIGGER tr_recordings_updated_at BEFORE UPDATE ON recordings "
        "FOR EACH ROW EXECUTE FUNCTION eca_set_updated_at()"
    )
    for stmt in user_isolation_ddl("recordings"):
        op.execute(stmt)
    op.execute(
        "ALTER TABLE meetings ADD COLUMN origin text NOT NULL DEFAULT 'calendar' "
        "CONSTRAINT ck_meetings_origin CHECK (origin IN ('calendar', 'upload'))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE meetings DROP COLUMN origin")
    for stmt in drop_user_isolation_ddl("recordings"):
        op.execute(stmt)
    op.execute("DROP TABLE recordings")
