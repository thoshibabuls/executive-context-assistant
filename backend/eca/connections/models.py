"""Tables owned by ``connections`` (BACKEND_DESIGN.md §5.1). Mirrors migration 0006."""

from __future__ import annotations

from sqlalchemy import Column, DateTime, Integer, MetaData, Table, Text
from sqlalchemy.dialects.postgresql import ARRAY, BYTEA, CITEXT
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

metadata = MetaData()

connections_table = Table(
    "connections",
    metadata,
    Column("id", PG_UUID(as_uuid=True), primary_key=True),
    Column("user_id", PG_UUID(as_uuid=True), nullable=False),
    Column("provider", Text, nullable=False),
    Column("account_email", CITEXT, nullable=False),
    Column("granted_scopes", ARRAY(Text), nullable=False),
    Column("refresh_token_ciphertext", BYTEA),
    Column("token_key_version", Integer),
    Column("status", Text, nullable=False),
    Column("last_error", Text),
    Column("created_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True)),
    Column("revoked_at", DateTime(timezone=True)),
)

sync_cursors_table = Table(
    "sync_cursors",
    metadata,
    Column("connection_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("resource", Text, primary_key=True),
    Column("user_id", PG_UUID(as_uuid=True), nullable=False),
    Column("cursor", Text),
    Column("cursor_obtained_at", DateTime(timezone=True)),
    Column("import_state", Text, nullable=False),
    Column("import_page_token", Text),
    Column("import_processed", Integer, nullable=False),
    Column("last_attempt_at", DateTime(timezone=True)),
    Column("last_success_at", DateTime(timezone=True)),
    Column("consecutive_failures", Integer, nullable=False),
    Column("status", Text, nullable=False),
    Column("lease_owner", Text),
    Column("lease_expires_at", DateTime(timezone=True)),
)
