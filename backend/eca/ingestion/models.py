"""Tables owned by ``ingestion`` (BACKEND_DESIGN.md §5.1). Mirrors migration 0007."""

from __future__ import annotations

from sqlalchemy import Boolean, Column, DateTime, MetaData, SmallInteger, Table, Text
from sqlalchemy.dialects.postgresql import ARRAY, BYTEA, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

metadata = MetaData()

source_items_table = Table(
    "source_items",
    metadata,
    Column("id", PG_UUID(as_uuid=True), primary_key=True),
    Column("user_id", PG_UUID(as_uuid=True), nullable=False),
    Column("connection_id", PG_UUID(as_uuid=True)),
    Column("kind", Text, nullable=False),
    Column("provider", Text, nullable=False),
    Column("external_id", Text, nullable=False),
    Column("external_thread_id", Text),
    Column("content_hash", BYTEA, nullable=False),
    Column("provider_version", Text),
    Column("categories", ARRAY(Text), nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("trashed", Boolean, nullable=False),
    Column("content", JSONB),
    Column("stage", Text, nullable=False),
    Column("stage_attempts", SmallInteger, nullable=False),
    Column("stage_updated_at", DateTime(timezone=True), nullable=False),
    Column("next_attempt_at", DateTime(timezone=True)),
    Column("last_error_code", Text),
    Column("raw_metadata", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True)),
    Column("deleted_at", DateTime(timezone=True)),
)
