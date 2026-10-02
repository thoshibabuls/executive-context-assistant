"""Tables owned by ``communication`` (BACKEND_DESIGN.md §5.1). Mirrors migration 0007."""

from __future__ import annotations

from sqlalchemy import Boolean, Column, DateTime, Integer, MetaData, Table, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

metadata = MetaData()

conversations_table = Table(
    "conversations",
    metadata,
    Column("id", PG_UUID(as_uuid=True), primary_key=True),
    Column("user_id", PG_UUID(as_uuid=True), nullable=False),
    Column("connection_id", PG_UUID(as_uuid=True)),
    Column("kind", Text, nullable=False),
    Column("external_thread_id", Text, nullable=False),
    Column("subject", Text),
    Column("first_message_at", DateTime(timezone=True)),
    Column("last_message_at", DateTime(timezone=True)),
    Column("last_inbound_at", DateTime(timezone=True)),
    Column("last_outbound_at", DateTime(timezone=True)),
    Column("awaiting", Text, nullable=False),
    Column("needs_reply", Boolean, nullable=False),
    Column("needs_reply_source", Text),
    Column("handled_by_user_at", DateTime(timezone=True)),
    Column("version", Integer, nullable=False),
)

messages_table = Table(
    "messages",
    metadata,
    Column("id", PG_UUID(as_uuid=True), primary_key=True),
    Column("user_id", PG_UUID(as_uuid=True), nullable=False),
    Column("source_item_id", PG_UUID(as_uuid=True), nullable=False),
    Column("conversation_id", PG_UUID(as_uuid=True), nullable=False),
    Column("rfc822_message_id", Text),
    Column("in_reply_to", Text),
    Column("sender_person_id", PG_UUID(as_uuid=True)),
    Column("direction", Text, nullable=False),
    Column("sent_at", DateTime(timezone=True), nullable=False),
    Column("subject", Text),
    Column("body_text", Text),
    Column("body_clean", Text),
    Column("snippet", Text),
    Column("is_bulk", Boolean, nullable=False),
    Column("prefilter_reason", Text),
    Column("triage", JSONB),
    Column("triage_extraction_id", PG_UUID(as_uuid=True)),
    Column("alias_source_item_ids", ARRAY(PG_UUID(as_uuid=True)), nullable=False),
    Column("deleted_at", DateTime(timezone=True)),
)

message_participants_table = Table(
    "message_participants",
    metadata,
    Column("message_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("person_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("role", Text, primary_key=True),
    Column("user_id", PG_UUID(as_uuid=True), nullable=False),
)
