"""User corrections as ``feedback_events`` (TECHNICAL_DESIGN.md §9.1; AI_EVALUATION.md).

Written in the same transaction as the correction by the module that owns the corrected entity
(work, communication, people). Append-only USER-AUTHORED rows: the evaluation set for
precision/recall drift and future learning. ``before`` and ``after`` hold field values only.
"""

from __future__ import annotations

import datetime
import decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Column, DateTime, MetaData, Table, Text, delete, insert
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from eca.platform.ids import uuid7
from eca.platform.uow import UnitOfWork

metadata = MetaData()

feedback_events_table = Table(
    "feedback_events",
    metadata,
    Column("id", PG_UUID(as_uuid=True), primary_key=True),
    Column("user_id", PG_UUID(as_uuid=True), nullable=False),
    Column("target_type", Text, nullable=False),
    Column("target_id", PG_UUID(as_uuid=True), nullable=False),
    Column("action", Text, nullable=False),
    Column("before", JSONB),
    Column("after", JSONB),
    Column("created_at", DateTime(timezone=True)),
    implicit_returning=False,
)


def _json_value(value: Any) -> Any:
    """Field values as JSON: dates as ISO 8601, IDs and decimals as strings."""
    if isinstance(value, datetime.datetime | datetime.date):
        return value.isoformat()
    if isinstance(value, UUID | decimal.Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_json_value(v) for v in value]
    return value


async def record_feedback(
    uow: UnitOfWork,
    *,
    target_type: str,
    target_id: UUID,
    action: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    await uow.session.execute(
        insert(feedback_events_table).values(
            id=uuid7(),
            user_id=uow.user_id,
            target_type=target_type,
            target_id=target_id,
            action=action,
            before=_json_value(before) if before is not None else None,
            after=_json_value(after) if after is not None else None,
        )
    )


async def purge_user_feedback(uow: UnitOfWork) -> None:
    await uow.session.execute(delete(feedback_events_table))
