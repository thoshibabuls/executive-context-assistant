"""Outbox dispatcher and infrastructure reconciler (BACKEND_DESIGN.md §7.2-§7.5).

The dispatcher claims pending rows with ``FOR UPDATE SKIP LOCKED`` in one worker-role
transaction (``app.user_id`` unset; the worker role reads all outbox rows through its own
policy). For each row it defers one Procrastinate job per registered handler with
``queueing_lock = lock = "<handler>:<event_id>"`` (``AlreadyEnqueued`` counts as success), then
marks the row ``dispatched``. Procrastinate writes jobs through its own connection, so a crash
between a defer and the commit leaves the row ``pending``; the duplicate defer that follows is
absorbed by the queueing lock, the job lock or ``event_consumptions`` (§7.4).

``outbox.attempts`` counts dispatch failures only (§7.3.2, §14.3).
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import procrastinate
import structlog
from procrastinate.exceptions import AlreadyEnqueued
from sqlalchemy import text

from eca.platform import crashpoints
from eca.platform.events import EventEnvelope, EventRegistry, UnregisteredEventType
from eca.platform.jobs import handler_task_name, job_lock_key
from eca.platform.uow import UnitOfWork, UnitOfWorkFactory

log = structlog.get_logger("eca.platform.dispatch")


@dataclass(frozen=True)
class DispatchPolicy:
    batch_size: int = 100
    tick_s: float = 1.0
    backoff_base_s: float = 1.0
    backoff_max_s: float = 300.0
    max_attempts: int = 10


def dispatch_backoff_s(
    failures: int, policy: DispatchPolicy, rand: Callable[[], float] = random.random
) -> float:
    """Delay after the n-th failed dispatch attempt: ``min(base · 2^(n-1) + U[0, 1), max)``."""
    if failures < 1:
        raise ValueError("failures must be >= 1")
    return float(min(policy.backoff_base_s * 2 ** (failures - 1) + rand(), policy.backoff_max_s))


@dataclass(frozen=True)
class DispatchResult:
    claimed: int
    dispatched: int
    errors: int


_CLAIM_SQL = text(
    """
    SELECT id, event_type, user_id, aggregate_type, aggregate_id, payload, correlation, created_at, attempts
      FROM outbox
     WHERE status = 'pending' AND next_attempt_at <= now()
       AND (CAST(:min_age_s AS float8) IS NULL OR created_at <= now() - make_interval(secs => :min_age_s))
     ORDER BY next_attempt_at, id
     LIMIT :limit
       FOR UPDATE SKIP LOCKED
    """
)
_MARK_DISPATCHED_SQL = text(
    "UPDATE outbox SET status = 'dispatched', dispatched_at = now(), last_error = NULL WHERE id = :id"
)
_MARK_FAILED_ATTEMPT_SQL = text(
    """
    UPDATE outbox
       SET attempts = :attempts,
           last_error = :last_error,
           next_attempt_at = now() + make_interval(secs => :delay_s),
           status = CASE WHEN :attempts >= :max_attempts THEN 'failed' ELSE 'pending' END
     WHERE id = :id
    """
)


class Dispatcher:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        registry: EventRegistry,
        job_app: procrastinate.App,
        policy: DispatchPolicy | None = None,
        *,
        rand: Callable[[], float] = random.random,
    ) -> None:
        self._uow_factory = uow_factory
        self._registry = registry
        self._job_app = job_app
        self.policy = policy or DispatchPolicy()
        self._rand = rand

    async def dispatch_once(self, *, min_age_s: float | None = None) -> DispatchResult:
        """Claim one batch and dispatch it in one transaction."""
        dispatched = errors = 0
        async with self._uow_factory(user_id=None) as uow:
            rows = (
                (
                    await uow.session.execute(
                        _CLAIM_SQL, {"min_age_s": min_age_s, "limit": self.policy.batch_size}
                    )
                )
                .mappings()
                .all()
            )
            crashpoints.hit("dispatch.after_claim")
            for row in rows:
                if await self._dispatch_row(uow, row):
                    dispatched += 1
                else:
                    errors += 1
            crashpoints.hit("dispatch.before_commit")
        return DispatchResult(claimed=len(rows), dispatched=dispatched, errors=errors)

    async def _dispatch_row(self, uow: UnitOfWork, row: Any) -> bool:
        event_id = row["id"]
        try:
            handlers = self._registry.handlers_for(row["event_type"])
            envelope = EventEnvelope(
                id=event_id,
                event_type=row["event_type"],
                user_id=row["user_id"],
                aggregate_type=row["aggregate_type"],
                aggregate_id=row["aggregate_id"],
                payload=row["payload"],
                correlation=row["correlation"],
                created_at=row["created_at"],
            ).model_dump(mode="json")
            for spec in handlers:
                key = job_lock_key(spec.name, event_id)
                deferrer = self._job_app.configure_task(
                    handler_task_name(spec.name),
                    allow_unknown=False,
                    queue=spec.queue,
                    lock=key,
                    queueing_lock=key,
                )
                try:
                    await deferrer.defer_async(envelope=envelope)
                    log.info("job_deferred", event_id=str(event_id), handler=spec.name)
                except AlreadyEnqueued:
                    log.info("job_already_enqueued", event_id=str(event_id), handler=spec.name)
            crashpoints.hit("dispatch.after_defer")
        except Exception as exc:
            await self._record_failure(uow, row, exc)
            return False
        await uow.session.execute(_MARK_DISPATCHED_SQL, {"id": event_id})
        return True

    async def _record_failure(self, uow: UnitOfWork, row: Any, exc: Exception) -> None:
        attempts = int(row["attempts"]) + 1
        code = "unregistered_event_type" if isinstance(exc, UnregisteredEventType) else "dispatch_error"
        await uow.session.execute(
            _MARK_FAILED_ATTEMPT_SQL,
            {
                "id": row["id"],
                "attempts": attempts,
                "last_error": f"{code}:{type(exc).__name__}",
                "delay_s": dispatch_backoff_s(attempts, self.policy, self._rand),
                "max_attempts": self.policy.max_attempts,
            },
        )
        fields = {"event_id": str(row["id"]), "attempts": attempts, "error_code": code}
        if attempts >= self.policy.max_attempts:
            log.error("outbox_event_failed", **fields, error_type=type(exc).__name__)
        else:
            log.warning("outbox_dispatch_retry", **fields, error_type=type(exc).__name__)

    async def run(self, stop: asyncio.Event) -> None:
        """Dispatch loop: again at once after a full batch, else wait one tick."""
        while not stop.is_set():
            try:
                result = await self.dispatch_once()
                full = result.claimed >= self.policy.batch_size
            except Exception as exc:  # a database outage must not end the worker
                log.error("dispatch_pass_failed", error_type=type(exc).__name__)
                full = False
            if not full:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self.policy.tick_s)


@dataclass(frozen=True)
class ReconcileReport:
    redispatched: int
    oldest_pending_age_s: float | None
    failed_count: int


_STATS_SQL = text(
    """
    SELECT (SELECT extract(epoch FROM now() - min(created_at)) FROM outbox WHERE status = 'pending')
             AS oldest_pending_age_s,
           (SELECT count(*) FROM outbox WHERE status = 'failed') AS failed_count
    """
)


async def reconcile_once(
    dispatcher: Dispatcher, uow_factory: UnitOfWorkFactory, *, min_age_s: float = 60.0
) -> ReconcileReport:
    """Infrastructure reconciler (§7.5): one dispatch pass over pending rows older than ``min_age_s``.

    Same dispatch step and ``SKIP LOCKED`` as the dispatcher, so it is safe beside a live one. It
    does not re-queue ``failed`` rows, touch dead jobs, or read any domain table.
    """
    redispatched = 0
    while True:
        result = await dispatcher.dispatch_once(min_age_s=min_age_s)
        redispatched += result.dispatched
        if result.claimed < dispatcher.policy.batch_size:
            break
    async with uow_factory(user_id=None) as uow:
        stats = (await uow.session.execute(_STATS_SQL)).mappings().one()
    oldest = stats["oldest_pending_age_s"]
    report = ReconcileReport(
        redispatched=redispatched,
        oldest_pending_age_s=float(oldest) if oldest is not None else None,
        failed_count=int(stats["failed_count"]),
    )
    log.info(
        "reconcile_pass",
        redispatched=report.redispatched,
        oldest_pending_age_s=report.oldest_pending_age_s,
        failed_count=report.failed_count,
    )
    return report
