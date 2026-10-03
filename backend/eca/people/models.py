"""Tables owned by ``people`` (BACKEND_DESIGN.md §5.1). Mirrors migrations 0006, 0007 and 0018."""

from __future__ import annotations

from sqlalchemy import REAL, Boolean, Column, DateTime, Integer, MetaData, SmallInteger, Table, Text
from sqlalchemy.dialects.postgresql import ARRAY, CITEXT, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

metadata = MetaData()

organizations_table = Table(
    "organizations",
    metadata,
    Column("id", PG_UUID(as_uuid=True), primary_key=True),
    Column("user_id", PG_UUID(as_uuid=True), nullable=False),
    Column("name", Text, nullable=False),
    Column("domain", CITEXT),
    Column("importance_user", SmallInteger),
    Column("origin", Text, nullable=False),
    Column("created_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True)),
    Column("deleted_at", DateTime(timezone=True)),
)

persons_table = Table(
    "persons",
    metadata,
    Column("id", PG_UUID(as_uuid=True), primary_key=True),
    Column("user_id", PG_UUID(as_uuid=True), nullable=False),
    Column("display_name", Text),
    Column("primary_email", CITEXT),
    Column("organization_id", PG_UUID(as_uuid=True)),
    Column("role_title", Text),
    Column("role_origin", Text),
    Column("relationship_type", Text),
    Column("importance_user", SmallInteger),
    Column("importance_inferred", REAL),
    Column("is_self", Boolean, nullable=False),
    Column("first_seen_at", DateTime(timezone=True)),
    Column("last_interaction_at", DateTime(timezone=True)),
    Column("last_inbound_at", DateTime(timezone=True)),
    Column("last_outbound_at", DateTime(timezone=True)),
    Column("interaction_stats", JSONB),
    Column("user_fields", ARRAY(Text), nullable=False),
    Column("profile_computed_at", DateTime(timezone=True)),
    Column("merged_into_id", PG_UUID(as_uuid=True)),
    Column("version", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True)),
    Column("deleted_at", DateTime(timezone=True)),
)

person_identifiers_table = Table(
    "person_identifiers",
    metadata,
    Column("id", PG_UUID(as_uuid=True), primary_key=True),
    Column("user_id", PG_UUID(as_uuid=True), nullable=False),
    Column("person_id", PG_UUID(as_uuid=True), nullable=False),
    Column("kind", Text, nullable=False),
    Column("value_normalized", Text, nullable=False),
    Column("source", Text, nullable=False),
    Column("confidence", REAL, nullable=False),
    Column("confirmed", Boolean, nullable=False),
    Column("created_at", DateTime(timezone=True)),
)

entity_mentions_table = Table(
    "entity_mentions",
    metadata,
    Column("id", PG_UUID(as_uuid=True), primary_key=True),
    Column("user_id", PG_UUID(as_uuid=True), nullable=False),
    Column("source_item_id", PG_UUID(as_uuid=True), nullable=False),
    Column("chunk_id", PG_UUID(as_uuid=True)),
    Column("evidence_id", PG_UUID(as_uuid=True)),
    Column("entity_type", Text, nullable=False),
    Column("entity_id", PG_UUID(as_uuid=True), nullable=False),
    Column("surface_text", Text, nullable=False),
    Column("confidence", REAL, nullable=False),
    Column("method", Text, nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True)),
)
