"""Tables owned by ``identity`` (BACKEND_DESIGN.md §5.1). Mirrors migration 0006."""

from __future__ import annotations

from sqlalchemy import Column, DateTime, MetaData, Table, Text
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

metadata = MetaData()

users_table = Table(
    "users",
    metadata,
    Column("id", PG_UUID(as_uuid=True), primary_key=True),
    Column("email", CITEXT, nullable=False),
    Column("display_name", Text, nullable=False),
    Column("timezone", Text, nullable=False),
    Column("work_hours", JSONB, nullable=False),
    Column("status", Text, nullable=False),
    Column("google_sub", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

deletion_jobs_table = Table(
    "deletion_jobs",
    metadata,
    Column("id", PG_UUID(as_uuid=True), primary_key=True),
    Column("user_id", PG_UUID(as_uuid=True), nullable=False),
    Column("kind", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("requested_at", DateTime(timezone=True)),
)
