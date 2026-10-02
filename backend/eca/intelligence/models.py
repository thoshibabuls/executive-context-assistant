"""Tables owned by ``intelligence`` (single writer, BACKEND_DESIGN.md §5.1): AI telemetry.

Mirrors migrations 0005 and 0008. ``ai_calls`` and ``ai_cost_rollups`` hold IDs and numbers only, never
prompt or output text (§5.5, §6.2).
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    Numeric,
    SmallInteger,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import BYTEA, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

metadata = MetaData()

ai_calls_table = Table(
    "ai_calls",
    metadata,
    Column("id", PG_UUID(as_uuid=True), primary_key=True),
    Column("user_id", PG_UUID(as_uuid=True), nullable=True),
    Column("role", Text, nullable=False),
    Column("inventory_id", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("prompt_version", Text, nullable=True),
    Column("schema_version", Text, nullable=True),
    Column("input_tokens", Integer, nullable=False),
    Column("cached_input_tokens", Integer, nullable=False),
    Column("output_tokens", Integer, nullable=False),
    Column("thinking_tokens", Integer, nullable=False),
    Column("audio_seconds", Numeric(10, 3), nullable=False),
    Column("latency_ms", Integer, nullable=False),
    Column("est_cost_usd", Numeric(14, 8), nullable=False),
    Column("status", Text, nullable=False),
    Column("error_code", Text, nullable=True),
    Column("attempt", SmallInteger, nullable=False),
    Column("is_fallback", Boolean, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    implicit_returning=False,
)

ai_cost_rollups_table = Table(
    "ai_cost_rollups",
    metadata,
    Column("bucket_start", DateTime(timezone=True), nullable=False),
    Column("user_id", PG_UUID(as_uuid=True), nullable=True),
    Column("role", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("calls", BigInteger, nullable=False),
    Column("failed_calls", BigInteger, nullable=False),
    Column("retry_calls", BigInteger, nullable=False),
    Column("fallback_calls", BigInteger, nullable=False),
    Column("input_tokens", BigInteger, nullable=False),
    Column("cached_input_tokens", BigInteger, nullable=False),
    Column("output_tokens", BigInteger, nullable=False),
    Column("thinking_tokens", BigInteger, nullable=False),
    Column("audio_seconds", Numeric(12, 3), nullable=False),
    Column("latency_ms_total", BigInteger, nullable=False),
    Column("est_cost_usd", Numeric(16, 8), nullable=False),
    Column("computed_at", DateTime(timezone=True), nullable=False),
)

extractions_table = Table(
    "extractions",
    metadata,
    Column("id", PG_UUID(as_uuid=True), primary_key=True),
    Column("user_id", PG_UUID(as_uuid=True), nullable=False),
    Column("source_item_id", PG_UUID(as_uuid=True), nullable=False),
    Column("content_hash", BYTEA, nullable=False),
    Column("pipeline", Text, nullable=False),
    Column("prompt_version", Text, nullable=False),
    Column("schema_version", Text, nullable=False),
    Column("model", Text),
    Column("input_hash", BYTEA, nullable=False),
    Column("candidate_map", JSONB, nullable=False),
    Column("parent_extraction_id", PG_UUID(as_uuid=True)),
    Column("status", Text, nullable=False),
    Column("attempts", SmallInteger, nullable=False),
    Column("output", JSONB),
    Column("error_code", Text),
    Column("apply_status", Text, nullable=False),
    Column("applied_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True)),
)
