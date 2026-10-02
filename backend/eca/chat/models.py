"""Tables owned by ``chat`` (BACKEND_DESIGN.md §5.1, §17.5). Mirrors migration 0017."""

from __future__ import annotations

from sqlalchemy import Column, DateTime, Integer, MetaData, Table, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

metadata = MetaData()
_U = PG_UUID(as_uuid=True)
_TS = DateTime(timezone=True)

chat_sessions_table = Table(
    "chat_sessions",
    metadata,
    Column("id", _U, primary_key=True),
    Column("user_id", _U, nullable=False),
    Column("title", Text),
    Column("scope", JSONB, nullable=False),
    Column("session_entities", JSONB, nullable=False),
    Column("version", Integer, nullable=False),
    Column("created_at", _TS),
    Column("last_active_at", _TS),
)

chat_messages_table = Table(
    "chat_messages",
    metadata,
    Column("id", _U, primary_key=True),
    Column("session_id", _U, nullable=False),
    Column("user_id", _U, nullable=False),
    Column("role", Text, nullable=False),
    Column("content", Text, nullable=False),
    Column("reply_to_id", _U),
    Column("scenario", Text),
    Column("answer_tier", Text),
    Column("claims", JSONB, nullable=False),
    Column("citations", JSONB, nullable=False),
    Column("confidence", Text),
    Column("provenance", JSONB),
    Column("retrieval_trace_id", _U),
    Column("ai_call_ids", ARRAY(_U), nullable=False),
    Column("created_at", _TS),
)
