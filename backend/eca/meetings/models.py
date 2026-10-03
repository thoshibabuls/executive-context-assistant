"""Tables owned by ``meetings`` (BACKEND_DESIGN.md §5.1). Mirrors migrations 0010 and 0024."""

from __future__ import annotations

from sqlalchemy import (
    REAL,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    SmallInteger,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import BYTEA, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

metadata = MetaData()
_U = PG_UUID(as_uuid=True)
_TS = DateTime(timezone=True)

meetings_table = Table(
    "meetings",
    metadata,
    Column("id", _U, primary_key=True),
    Column("user_id", _U, nullable=False),
    Column("source_item_id", _U, nullable=False),
    Column("title", Text),
    Column("description", Text),
    Column("starts_at", _TS, nullable=False),
    Column("ends_at", _TS, nullable=False),
    Column("timezone", Text),
    Column("series_key", Text),
    Column("conference_uri", Text),
    Column("organizer_person_id", _U),
    Column("status", Text, nullable=False),
    Column("processing_status", Text, nullable=False),
    Column("summary", JSONB),
    Column("project_hint", Text),
    Column("origin", Text, nullable=False),
    Column("prep_brief", JSONB),
    Column("prep_brief_version", Integer, nullable=False),
    Column("version", Integer, nullable=False),
    Column("deleted_at", _TS),
)

meeting_participants_table = Table(
    "meeting_participants",
    metadata,
    Column("meeting_id", _U, primary_key=True),
    Column("person_id", _U, primary_key=True),
    Column("user_id", _U, nullable=False),
    Column("response_status", Text),
    Column("is_organizer", Boolean, nullable=False),
    Column("attended", Boolean),
    Column("origin", Text, nullable=False),
)

recordings_table = Table(
    "recordings",
    metadata,
    Column("id", _U, primary_key=True),
    Column("user_id", _U, nullable=False),
    Column("kind", Text, nullable=False),
    Column("mime", Text, nullable=False),
    Column("bytes", BigInteger, nullable=False),
    Column("sha256", BYTEA, nullable=False),
    Column("title", Text),
    Column("occurred_at", _TS),
    Column("storage_key", Text, nullable=False),
    Column("audio_storage_key", Text),
    Column("source_item_id", _U),
    Column("meeting_id", _U),
    Column("status", Text, nullable=False),
    Column("failed_stage", Text),
    Column("error_code", Text),
    Column("stage_attempts", SmallInteger, nullable=False),
    Column("next_attempt_at", _TS),
    Column("duration_s", REAL),
    Column("transcription_model", Text),
    Column("transcript_hash", BYTEA),
    Column("provider_file_ref", JSONB),
    Column("provider_file_expires_at", _TS),
    Column("upload_expires_at", _TS, nullable=False),
    Column("processed_at", _TS),
    Column("raw_purged_at", _TS),
    Column("version", Integer, nullable=False),
    Column("created_at", _TS),
    Column("updated_at", _TS),
)
