"""Tables owned by ``projects`` (BACKEND_DESIGN.md §5.1, §17.5). Mirrors migration 0015."""

from __future__ import annotations

from sqlalchemy import Column, DateTime, Integer, MetaData, SmallInteger, Table, Text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

metadata = MetaData()
_U = PG_UUID(as_uuid=True)
_TS = DateTime(timezone=True)

projects_table = Table(
    "projects",
    metadata,
    Column("id", _U, primary_key=True),
    Column("user_id", _U, nullable=False),
    Column("name", Text, nullable=False),
    Column("description", Text),
    Column("aliases", ARRAY(Text), nullable=False),
    Column("hint_key", Text),
    Column("status", Text, nullable=False),
    Column("importance_user", SmallInteger),
    Column("origin", Text, nullable=False),
    Column("verification_status", Text, nullable=False),
    Column("suggestion_sources", Integer),
    Column("user_fields", ARRAY(Text), nullable=False),
    Column("version", Integer, nullable=False),
    Column("created_at", _TS),
    Column("updated_at", _TS),
    Column("deleted_at", _TS),
)

project_members_table = Table(
    "project_members",
    metadata,
    Column("project_id", _U, primary_key=True),
    Column("person_id", _U, primary_key=True),
    Column("user_id", _U, nullable=False),
    Column("role", Text),
    Column("origin", Text, nullable=False),
)
