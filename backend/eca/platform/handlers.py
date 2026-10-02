"""Handler wrapper: effective exactly-once database effects (BACKEND_DESIGN.md §7.4).

One worker-role unit of work per job, with ``app.user_id`` set to the event's user (unset for
system events). Its first statement records ``event_consumptions(event_id, handler)`` with
``ON CONFLICT DO NOTHING``; if the row already exists the handler returns without effects. A
concurrent transaction holding the same key makes the insert wait until it ends. The handler's
writes and the consumption record commit together, or roll back together on any exception
(Procrastinate then retries the job).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import structlog
from sqlalchemy.dialects.postgresql import insert

from eca.platform import crashpoints
from eca.platform.events import EventEnvelope, EventRegistry, HandlerContext
from eca.platform.outbox import event_consumptions_table
from eca.platform.uow import UnitOfWorkFactory

log = structlog.get_logger("eca.platform.handlers")


async def run_handler(
    uow_factory: UnitOfWorkFactory,
    registry: EventRegistry,
    handler_name: str,
    envelope_args: Mapping[str, Any],
    *,
    attempt: int,
) -> bool:
    """Run one handler for one event. Returns False when the event was already consumed."""
    spec = registry.handler(handler_name)
    envelope = EventEnvelope.model_validate(envelope_args)
    if envelope.event_type != spec.event_type:
        raise ValueError(f"Handler {handler_name!r} does not handle {envelope.event_type!r}")
    payload = registry.payload_model(envelope.event_type).model_validate(envelope.payload)

    with structlog.contextvars.bound_contextvars(event_id=str(envelope.id), handler=handler_name):
        async with uow_factory(user_id=envelope.user_id) as uow:
            claimed = await uow.session.execute(
                insert(event_consumptions_table)
                .values(event_id=envelope.id, handler=handler_name)
                .on_conflict_do_nothing(index_elements=["event_id", "handler"])
                .returning(event_consumptions_table.c.event_id)
            )
            if claimed.first() is None:
                log.info("handler_duplicate_skipped", attempt=attempt)
                return False
            crashpoints.hit("handler.after_consumption")
            await spec.func(HandlerContext(uow=uow, envelope=envelope, payload=payload, attempt=attempt))
            crashpoints.hit("handler.before_commit")
        crashpoints.hit("handler.after_commit")
        log.info("handler_applied", attempt=attempt)
        return True
