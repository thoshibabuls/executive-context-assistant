"""Audit records (``audit_log``, owned by ``privacy``; written through this helper, §6.2).

Content-free: action names, target IDs and small metadata only; never tokens, email bodies or
other source content.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Column, DateTime, MetaData, Table, Text, insert
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from eca.platform.ids import uuid7
from eca.platform.uow import UnitOfWork

metadata = MetaData()

audit_log_table = Table(
    "audit_log",
    metadata,
    Column("id", PG_UUID(as_uuid=True), primary_key=True),
    Column("user_id", PG_UUID(as_uuid=True)),
    Column("actor", Text, nullable=False),
    Column("action", Text, nullable=False),
    Column("target_type", Text),
    Column("target_id", PG_UUID(as_uuid=True)),
    Column("ip", Text),
    Column("metadata", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True)),
    implicit_returning=False,
)


async def record_audit(
    uow: UnitOfWork,
    action: str,
    *,
    actor: str = "user",
    target_type: str | None = None,
    target_id: UUID | None = None,
    ip: str | None = None,
    metadata: dict[str, Any] | None = None,
    anonymous: bool = False,
) -> None:
    """Insert one audit row for the unit of work's user (NULL before sign-in, or ``anonymous``
    for the content-free record that outlives a deleted account)."""
    await uow.session.execute(
        insert(audit_log_table).values(
            id=uuid7(),
            user_id=None if anonymous else uow.user_id,
            actor=actor,
            action=action,
            target_type=target_type,
            target_id=target_id,
            ip=ip,
            metadata=metadata or {},
        )
    )
