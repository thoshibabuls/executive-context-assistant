"""Inline event executor: dispatch outbox rows and run their handlers in-process.

Used by integration tests and the evaluation runners (CONTEXT_EVALUATION.md L2 replay), which
need the real handlers and transactions but not Procrastinate or a separate worker process. It
claims pending rows with the dispatcher's ``FOR UPDATE SKIP LOCKED`` query (worker role), marks
them dispatched in the same transaction, then runs each subscribed handler through
:func:`eca.platform.handlers.run_handler`, so ``event_consumptions`` and the handler modes behave
exactly as in production. The production worker never uses this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from eca.platform.events import EventEnvelope, EventRegistry, Resources
from eca.platform.handlers import run_handler
from eca.platform.uow import UnitOfWorkFactory

_CLAIM_SQL = text(
    """
    SELECT id, user_id, event_type, aggregate_type, aggregate_id, payload, correlation, created_at
      FROM outbox
     WHERE status = 'pending'
     ORDER BY created_at, id
     LIMIT :limit
       FOR UPDATE SKIP LOCKED
    """
)
_MARK_SQL = text("UPDATE outbox SET status = 'dispatched', dispatched_at = now() WHERE id = ANY(:ids)")


@dataclass
class InlineStats:
    events: int = 0
    handler_runs: int = 0
    duplicates_skipped: int = 0


class InlineExecutor:
    def __init__(self, uow_factory: UnitOfWorkFactory, registry: EventRegistry, resources: Resources) -> None:
        self._uow_factory = uow_factory
        self._registry = registry
        self._resources = resources
        self.stats = InlineStats()

    async def run_envelope(self, envelope: dict[str, Any]) -> None:
        """Run every handler of one event envelope (a re-delivery runs them again)."""
        for spec in self._registry.handlers_for(envelope["event_type"]):
            applied = await run_handler(
                self._uow_factory, self._registry, spec.name, envelope, attempt=0, resources=self._resources
            )
            self.stats.handler_runs += 1
            if not applied:
                self.stats.duplicates_skipped += 1

    async def drain(self, *, max_rounds: int = 10_000, batch: int = 50) -> int:
        """Dispatch and run until no pending outbox row is left; returns events processed."""
        processed = 0
        for _ in range(max_rounds):
            async with self._uow_factory(user_id=None) as uow:
                rows = (await uow.session.execute(_CLAIM_SQL, {"limit": batch})).mappings().all()
                if rows:
                    await uow.session.execute(_MARK_SQL, {"ids": [r["id"] for r in rows]})
            if not rows:
                return processed
            for row in rows:
                envelope = EventEnvelope.model_validate(dict(row)).model_dump(mode="json")
                await self.run_envelope(envelope)
                processed += 1
                self.stats.events += 1
        raise RuntimeError("inline executor did not converge (event loop between handlers?)")
