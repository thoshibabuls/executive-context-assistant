"""Tables owned by ``privacy`` (TECHNICAL_DESIGN.md §9.1). ``audit_log`` is declared in
``platform.audit`` (written through that helper by every module)."""

from __future__ import annotations

from sqlalchemy import Column, DateTime, MetaData, Table, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

metadata = MetaData()

deletion_jobs_table = Table(
    "deletion_jobs",
    metadata,
    Column("id", PG_UUID(as_uuid=True), primary_key=True),
    Column("user_id", PG_UUID(as_uuid=True), nullable=False),
    Column("kind", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("progress", JSONB, nullable=False),
    Column("requested_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True)),
)
