"""Handler wrapper: effective exactly-once database effects (BACKEND_DESIGN.md §7.4).

One worker-role unit of work per job, with ``app.user_id`` set to the event's user (unset for
system events). Its first statement records ``event_consumptions(event_id, handler)`` with
``ON CONFLICT DO NOTHING``; if the row already exists the handler returns without effects. A
concurrent transaction holding the same key makes the insert wait until it ends. The handler's
writes and the consumption record commit together, or roll back together on any exception
(Procrastinate then retries the job).

``natural_key`` handlers (§7.4) make external calls: no wrapper transaction and no consumption
row; the handler runs its own short transactions and is idempotent by natural keys.

User gate (§13.3): an event of a user whose row is ``deleting`` or gone is skipped (no effects,
no retry), except by handlers registered with ``runs_while_deleting`` (the deletion job).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert

from eca.platform import crashpoints
from eca.platform.events import EventEnvelope, EventRegistry, HandlerContext, Resources
from eca.platform.outbox import event_consumptions_table
from eca.platform.uow import UnitOfWorkFactory

log = structlog.get_logger("eca.platform.handlers")

# The worker reads ``users(id, status)`` through ``users_worker_enumerate`` (§7.6).
_USER_ACTIVE_SQL = text("SELECT status = 'active' FROM users WHERE id = :user_id")


async def _user_active(uow_factory: UnitOfWorkFactory, user_id: object) -> bool:
    async with uow_factory(user_id=None) as uow:
        row = (await uow.session.execute(_USER_ACTIVE_SQL, {"user_id": user_id})).first()
    return bool(row and row[0])


async def run_handler(
    uow_factory: UnitOfWorkFactory,
    registry: EventRegistry,
    handler_name: str,
    envelope_args: Mapping[str, Any],
    *,
    attempt: int,
    resources: Resources | None = None,
) -> bool:
    """Run one handler for one event. Returns False when the event was already consumed."""
    spec = registry.handler(handler_name)
    envelope = EventEnvelope.model_validate(envelope_args)
    if envelope.event_type != spec.event_type:
        raise ValueError(f"Handler {handler_name!r} does not handle {envelope.event_type!r}")
    payload = registry.payload_model(envelope.event_type).model_validate(envelope.payload)

    resources = resources or Resources()

    with structlog.contextvars.bound_contextvars(event_id=str(envelope.id), handler=handler_name):
        if (
            envelope.user_id is not None
            and not spec.runs_while_deleting
            and not await _user_active(uow_factory, envelope.user_id)
        ):
            log.info("handler_skipped_inactive_user", attempt=attempt)
            return False
        if spec.mode == "natural_key":
            await spec.func(
                HandlerContext(
                    uow=None,
                    envelope=envelope,
                    payload=payload,
                    attempt=attempt,
                    uow_factory=uow_factory,
                    resources=resources,
                )
            )
            log.info("handler_applied", attempt=attempt, mode="natural_key")
            return True
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
            await spec.func(
                HandlerContext(
                    uow=uow,
                    envelope=envelope,
                    payload=payload,
                    attempt=attempt,
                    uow_factory=uow_factory,
                    resources=resources,
                )
            )
            crashpoints.hit("handler.before_commit")
        crashpoints.hit("handler.after_commit")
        log.info("handler_applied", attempt=attempt)
        return True
