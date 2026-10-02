"""Procrastinate integration (pinned ``procrastinate==3.10.0``; BACKEND_DESIGN.md §14.3, §15).

* One Procrastinate task per registered handler (``eca.handler.<name>``), wrapped by
  :func:`eca.platform.handlers.run_handler`, retried by :class:`HandlerRetryStrategy`.
* Infrastructure tasks on the ``schedule`` queue: ``eca.reconcile`` (every 5 min) and
  ``eca.recover_stalled_jobs`` (every minute, and once at worker start, see
  :func:`recover_stalled_jobs`).
* Module periodic tasks declared as :class:`PeriodicTaskSpec` (e.g. ``intelligence``'s
  ``cost_rollup``) and registered by the worker with :func:`register_periodic_tasks`.

Procrastinate never takes part in a SQLAlchemy transaction; it uses its own psycopg pool,
connected as the worker role.
"""

from __future__ import annotations

import datetime
import random
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import procrastinate
import structlog
from procrastinate import JobContext
from procrastinate.jobs import Job

from eca.platform.errors import AuthRevoked, Gone, ValidationFailed
from eca.platform.events import EventRegistry, Resources
from eca.platform.handlers import run_handler
from eca.platform.uow import UnitOfWorkFactory

log = structlog.get_logger("eca.platform.jobs")

RECONCILE_TASK = "eca.reconcile"
RECOVER_STALLED_TASK = "eca.recover_stalled_jobs"


def handler_task_name(handler_name: str) -> str:
    return f"eca.handler.{handler_name}"


def job_lock_key(handler_name: str, event_id: UUID) -> str:
    """``queueing_lock`` and ``lock`` of a handler job: ``"<handler>:<event_id>"`` (§7.3.2)."""
    return f"{handler_name}:{event_id}"


class HandlerRetryStrategy(procrastinate.BaseRetryStrategy):
    """Handler job retries (§14.3). Procrastinate's ``RetryStrategy`` cannot express this policy.

    Run n = ``job.attempts + 1`` (Procrastinate counts earlier runs). After a failed run n < 8 the
    job is retried after ``min(base · 2^(n-1) + U[0, 1), max_delay)`` seconds. After run 8, or a
    non-retryable error, there is no retry: Procrastinate marks the job ``failed`` (a dead job).
    """

    def __init__(
        self,
        *,
        base_s: float = 5.0,
        max_delay_s: float = 600.0,
        max_attempts: int = 8,
        non_retryable: Iterable[type[BaseException]] = (ValidationFailed, AuthRevoked, Gone),
        clock: Callable[[], datetime.datetime] = lambda: datetime.datetime.now(datetime.UTC),
        rand: Callable[[], float] = random.random,
    ) -> None:
        if base_s <= 0 or max_delay_s <= 0 or max_attempts < 1:
            raise ValueError("invalid retry policy")
        self.base_s = base_s
        self.max_delay_s = max_delay_s
        self.max_attempts = max_attempts
        self.non_retryable = tuple(non_retryable)
        self._clock = clock
        self._rand = rand

    def delay_s(self, run: int) -> float:
        return float(min(self.base_s * 2 ** (run - 1) + self._rand(), self.max_delay_s))

    def get_retry_decision(self, *, exception: BaseException, job: Job) -> procrastinate.RetryDecision | None:
        run = job.attempts + 1
        if isinstance(exception, self.non_retryable) or run >= self.max_attempts:
            envelope = job.task_kwargs.get("envelope")
            event_id = envelope.get("id") if isinstance(envelope, dict) else None
            log.error(
                "handler_job_dead",
                job_id=job.id,
                task=job.task_name,
                event_id=event_id,
                run=run,
                error_type=type(exception).__name__,
            )
            return None
        return procrastinate.RetryDecision(
            retry_at=self._clock() + datetime.timedelta(seconds=self.delay_s(run))
        )


def create_job_app(conninfo: str, *, pool_max_size: int) -> procrastinate.App:
    """A Procrastinate app on its own psycopg pool (``conninfo`` must be the worker role)."""
    connector = procrastinate.PsycopgConnector(conninfo=conninfo, min_size=1, max_size=pool_max_size)
    return procrastinate.App(connector=connector)


def register_handler_tasks(
    app: procrastinate.App,
    registry: EventRegistry,
    uow_factory: UnitOfWorkFactory,
    retry: procrastinate.BaseRetryStrategy,
    resources: Resources | None = None,
) -> None:
    for spec in registry.handlers():

        def make(handler_name: str) -> Callable[..., Awaitable[None]]:
            async def handler_job(context: JobContext, envelope: dict[str, Any]) -> None:
                assert context.job is not None
                with structlog.contextvars.bound_contextvars(job_id=context.job.id):
                    await run_handler(
                        uow_factory,
                        registry,
                        handler_name,
                        envelope,
                        attempt=context.job.attempts,
                        resources=resources,
                    )

            return handler_job

        app.task(name=handler_task_name(spec.name), queue=spec.queue, pass_context=True, retry=retry)(
            make(spec.name)
        )


async def recover_stalled_jobs(app: procrastinate.App, *, stalled_timeout_s: float) -> int:
    """Re-queue jobs of workers whose heartbeat is older than ``stalled_timeout_s``.

    ``retry_job`` increments the job's attempts, so a worker death counts as one attempt.
    """
    stalled = list(await app.job_manager.get_stalled_jobs(seconds_since_heartbeat=stalled_timeout_s))
    for job in stalled:
        await app.job_manager.retry_job(job)
        log.warning("stalled_job_recovered", job_id=job.id, task=job.task_name, attempts=job.attempts + 1)
    await app.job_manager.prune_stalled_workers(stalled_timeout_s)
    return len(stalled)


@dataclass(frozen=True)
class PeriodicTaskSpec:
    """A module's periodic job (BACKEND_DESIGN.md §5.5).

    ``run`` receives the worker's unit-of-work factory and the scheduled tick (UTC). The task runs
    under a lock named after ``periodic_id``, so two ticks never overlap.
    """

    name: str
    periodic_id: str
    cron: str
    queue: str
    run: Callable[[UnitOfWorkFactory, datetime.datetime], Awaitable[None]]


def register_periodic_tasks(
    app: procrastinate.App, specs: Iterable[PeriodicTaskSpec], uow_factory: UnitOfWorkFactory
) -> None:
    for spec in specs:

        def make(s: PeriodicTaskSpec) -> Callable[[int], Awaitable[None]]:
            async def periodic_job(timestamp: int) -> None:
                await s.run(uow_factory, datetime.datetime.fromtimestamp(timestamp, datetime.UTC))

            return periodic_job

        task = app.task(
            name=spec.name, queue=spec.queue, lock=spec.periodic_id, queueing_lock=spec.periodic_id
        )
        app.periodic(cron=spec.cron, periodic_id=spec.periodic_id)(task(make(spec)))


def register_infrastructure_tasks(
    app: procrastinate.App,
    *,
    reconcile: Callable[[], Awaitable[object]],
    stalled_timeout_s: float,
) -> None:
    @app.periodic(cron="*/5 * * * *", periodic_id="reconcile")
    @app.task(name=RECONCILE_TASK, queue="schedule", lock="reconcile", queueing_lock="reconcile")
    async def reconcile_task(timestamp: int) -> None:
        await reconcile()

    @app.periodic(cron="* * * * *", periodic_id="recover_stalled_jobs")
    @app.task(
        name=RECOVER_STALLED_TASK,
        queue="schedule",
        lock="recover_stalled_jobs",
        queueing_lock="recover_stalled_jobs",
    )
    async def recover_task(timestamp: int) -> None:
        await recover_stalled_jobs(app, stalled_timeout_s=stalled_timeout_s)
