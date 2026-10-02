"""Tables owned by ``meetings`` (BACKEND_DESIGN.md §5.1). Mirrors migration 0010."""

from __future__ import annotations

from sqlalchemy import Boolean, Column, DateTime, Integer, MetaData, Table, Text
from sqlalchemy.dialects.postgresql import JSONB
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
