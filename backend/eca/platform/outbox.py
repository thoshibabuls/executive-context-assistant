"""Transactional outbox: publish, operator retry (BACKEND_DESIGN.md §7.3).

``publish`` writes one ``outbox`` row in the caller's unit of work, so the business change and
the event commit or roll back together. It works for the API role, which has INSERT and no
SELECT on ``outbox`` (§7.3.3): one SQLAlchemy Core INSERT with an explicit column list, no
RETURNING, no ON CONFLICT, nothing read back. The lifecycle columns come from DDL defaults.
"""

from __future__ import annotations

from uuid import UUID

import structlog
from sqlalchemy import Column, DateTime, Integer, MetaData, Table, Text, insert, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from eca.platform.events import EventRegistry, NewEvent, default_registry
from eca.platform.ids import uuid7
from eca.platform.uow import UnitOfWork

metadata = MetaData()

# Mirrors migration 0003. Only the columns the application writes or reads are declared with
# their types; server defaults stay in the DDL and are never fetched by the publishing role.
outbox_table = Table(
    "outbox",
    metadata,
    Column("id", PG_UUID(as_uuid=True), primary_key=True),
    Column("user_id", PG_UUID(as_uuid=True), nullable=True),
    Column("event_type", Text, nullable=False),
    Column("aggregate_type", Text, nullable=False),
    Column("aggregate_id", PG_UUID(as_uuid=True), nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("correlation", JSONB, nullable=False),
    Column("status", Text, nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("next_attempt_at", DateTime(timezone=True), nullable=False),
    Column("last_error", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("dispatched_at", DateTime(timezone=True), nullable=True),
    implicit_returning=False,
)

event_consumptions_table = Table(
    "event_consumptions",
    metadata,
    Column("event_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("handler", Text, primary_key=True),
    Column("consumed_at", DateTime(timezone=True), nullable=False),
)


def _correlation() -> dict[str, str]:
    request_id = structlog.contextvars.get_contextvars().get("request_id")
    return {"request_id": request_id} if isinstance(request_id, str) else {}


async def publish(uow: UnitOfWork, event: NewEvent, *, registry: EventRegistry = default_registry) -> UUID:
    """Insert the event into ``outbox`` in ``uow``'s transaction; returns the event ID.

    ``user_id`` is the unit of work's user (NULL for a system unit of work), never caller input.
    The payload must be an instance of the model registered for the event type.
    """
    model = registry.payload_model(event.event_type)
    if type(event.payload) is not model:
        raise TypeError(f"Payload for {event.event_type!r} must be {model.__name__}")
    event_id = uuid7()
    await uow.session.execute(
        insert(outbox_table).values(
            id=event_id,
            user_id=uow.user_id,
            event_type=event.event_type,
            aggregate_type=event.aggregate_type,
            aggregate_id=event.aggregate_id,
            payload=event.payload.model_dump(mode="json"),
            correlation=_correlation(),
        )
    )
    return event_id


_RETRY_FAILED_SQL = text(
    """
    UPDATE outbox
       SET status = 'pending', attempts = 0, next_attempt_at = now(), last_error = NULL
     WHERE status = 'failed' AND (CAST(:event_id AS uuid) IS NULL OR id = :event_id)
    """
)


async def retry_failed(uow: UnitOfWork, *, event_id: UUID | None = None) -> int:
    """Operator action ``eca ops outbox retry``: ``failed`` → ``pending`` with ``attempts = 0``."""
    result = await uow.session.execute(_RETRY_FAILED_SQL, {"event_id": event_id})
    count: int = result.rowcount  # type: ignore[attr-defined]
    return count
