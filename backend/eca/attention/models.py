"""Tables owned by ``attention`` (BACKEND_DESIGN.md §5.1, §17.6). Mirrors migrations 0019 and 0020."""

from __future__ import annotations

from sqlalchemy import (
    REAL,
    Boolean,
    Column,
    DateTime,
    Integer,
    LargeBinary,
    MetaData,
    SmallInteger,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

metadata = MetaData()

reminders_table = Table(
    "reminders",
    metadata,
    Column("id", PG_UUID(as_uuid=True), primary_key=True),
    Column("user_id", PG_UUID(as_uuid=True), nullable=False),
    Column("item_type", Text, nullable=False),
    Column("item_id", PG_UUID(as_uuid=True), nullable=False),
    Column("person_id", PG_UUID(as_uuid=True)),
    Column("reminder_type", Text, nullable=False),
    Column("slot", Text, nullable=False),
    Column("fire_at", DateTime(timezone=True), nullable=False),
    Column("first_fire_at", DateTime(timezone=True), nullable=False),
    Column("state", Text, nullable=False),
    Column("fingerprint", LargeBinary, nullable=False),
    Column("material_key", LargeBinary, nullable=False),
    Column("reason", JSONB, nullable=False),
    Column("priority", REAL, nullable=False),
    Column("proactive_eligible", Boolean, nullable=False),
    Column("proactive", Boolean),
    Column("delivery_seq", SmallInteger, nullable=False),
    Column("delivered_at", DateTime(timezone=True)),
    Column("snoozed_until", DateTime(timezone=True)),
    Column("snooze_count", SmallInteger, nullable=False),
    Column("closed_at", DateTime(timezone=True)),
    Column("closed_reason", Text),
    Column("version", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True)),
)

notifications_table = Table(
    "notifications",
    metadata,
    Column("id", PG_UUID(as_uuid=True), primary_key=True),
    Column("user_id", PG_UUID(as_uuid=True), nullable=False),
    Column("reminder_id", PG_UUID(as_uuid=True), nullable=False),
    Column("channel", Text, nullable=False),
    Column("seq", SmallInteger, nullable=False),
    Column("state", Text, nullable=False),
    Column("attempts", SmallInteger, nullable=False),
    Column("last_error", Text),
    Column("sent_at", DateTime(timezone=True)),
    Column("read_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True)),
)

push_subscriptions_table = Table(
    "push_subscriptions",
    metadata,
    Column("id", PG_UUID(as_uuid=True), primary_key=True),
    Column("user_id", PG_UUID(as_uuid=True), nullable=False),
    Column("endpoint", Text, nullable=False),
    Column("expires_at", DateTime(timezone=True)),
    Column("user_agent", Text),
    Column("last_success_at", DateTime(timezone=True)),
    Column("failure_count", SmallInteger, nullable=False),
    Column("revoked_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True)),
)
