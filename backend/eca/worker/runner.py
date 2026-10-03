"""Worker process: dispatcher loop, Procrastinate queue workers, infrastructure tasks.

``run_worker`` is the single public way to run the worker. The production entry point
(``python -m eca.worker``) calls it with the default registry and ``WorkerConfig()``; it has no
option, setting or environment variable that loads extra modules. Test code calls it directly
with its own registry and timings.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
from collections.abc import Awaitable, Callable, Sequence
from typing import Literal

import structlog
from sqlalchemy import text
from sqlalchemy.engine import make_url

from eca.platform import crashpoints
from eca.platform.config import Settings
from eca.platform.db import create_engine, create_session_factory
from eca.platform.dispatch import Dispatcher, reconcile_once
from eca.platform.events import EventRegistry, Resources
from eca.platform.jobs import (
    PeriodicTaskSpec,
    create_job_app,
    recover_stalled_jobs,
    register_handler_tasks,
    register_infrastructure_tasks,
    register_periodic_tasks,
)
from eca.platform.uow import UnitOfWorkFactory
from eca.worker.config import WorkerConfig

log = structlog.get_logger("eca.worker")

Mode = Literal["all", "dispatcher", "jobs"]
ResourcesBuilder = Callable[[UnitOfWorkFactory], Resources]
ReconcileHook = Callable[[UnitOfWorkFactory], Awaitable[object]]


class WorkerStartupError(RuntimeError):
    """The worker refuses to start (configuration or database role problem)."""


def _worker_url(settings: Settings) -> str:
    if settings.api_worker_database_url is None or not settings.api_worker_database_url.get_secret_value():
        raise WorkerStartupError("API_WORKER_DATABASE_URL is required to run the worker")
    return settings.api_worker_database_url.get_secret_value()


def _conninfo(url: str) -> str:
    """psycopg conninfo (plain ``postgresql://`` URL) for Procrastinate's pool."""
    return make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)


async def run_worker(
    *,
    registry: EventRegistry,
    config: WorkerConfig,
    settings: Settings,
    mode: Mode = "all",
    stop: asyncio.Event | None = None,
    periodic: Sequence[PeriodicTaskSpec] = (),
    build_resources: ResourcesBuilder | None = None,
    reconcile_hooks: Sequence[ReconcileHook] = (),
) -> None:
    """Run until ``stop`` is set or SIGINT/SIGTERM arrives.

    ``build_resources`` gives handlers their process-level dependencies (AI client, connector
    registry; §5.6). ``reconcile_hooks`` run after each infrastructure reconcile pass (the
    per-user source-item stage scan, §7.5).
    """
    crashpoints.configure(is_production=settings.is_production)
    url = _worker_url(settings)
    stop = stop or asyncio.Event()

    engine = create_engine(url, pool_size=config.db_pool_size, max_overflow=config.db_max_overflow)
    try:
        async with engine.connect() as conn:
            current = (await conn.execute(text("SELECT current_user"))).scalar_one()
        if current != settings.api_db_worker_role or current == settings.api_db_runtime_role:
            raise WorkerStartupError(
                f"The worker must connect as the worker role {settings.api_db_worker_role!r}, "
                f"not {current!r} (BACKEND_DESIGN.md §7.6)"
            )
        uow_factory = UnitOfWorkFactory(create_session_factory(engine))
        job_app = create_job_app(_conninfo(url), pool_max_size=config.job_pool_max_size)
        resources = build_resources(uow_factory) if build_resources is not None else Resources()
        register_handler_tasks(job_app, registry, uow_factory, config.handler_retry, resources)
        dispatcher = Dispatcher(uow_factory, registry, job_app, config.dispatch)

        async def reconcile() -> None:
            await reconcile_once(dispatcher, uow_factory, min_age_s=config.reconcile_min_age_s)
            for hook in reconcile_hooks:
                await hook(uow_factory)

        register_infrastructure_tasks(
            job_app, reconcile=reconcile, stalled_timeout_s=config.stalled_timeout_s
        )
        register_periodic_tasks(job_app, periodic, uow_factory)

        async with job_app.open_async():
            _install_signal_handlers(stop)
            tasks: list[asyncio.Task[None]] = []
            if mode in ("all", "dispatcher"):
                tasks.append(asyncio.create_task(dispatcher.run(stop), name="dispatcher"))
            if mode in ("all", "jobs"):
                await recover_stalled_jobs(job_app, stalled_timeout_s=config.stalled_timeout_s)
                for queue, concurrency in config.queues.items():
                    tasks.append(
                        asyncio.create_task(
                            job_app.run_worker_async(
                                queues=[queue],
                                name=f"eca-{queue}",
                                concurrency=concurrency,
                                install_signal_handlers=False,
                                update_heartbeat_interval=config.heartbeat_interval_s,
                                stalled_worker_timeout=config.stalled_timeout_s,
                                fetch_job_polling_interval=config.fetch_job_polling_interval_s,
                                shutdown_graceful_timeout=config.shutdown_graceful_timeout_s,
                            ),
                            name=f"queue-{queue}",
                        )
                    )
            log.info(
                "worker_started", mode=mode, queues=sorted(config.queues) if mode != "dispatcher" else []
            )
            await _supervise(tasks, stop)
    finally:
        await engine.dispose()
    log.info("worker_stopped", mode=mode)


async def _supervise(tasks: list[asyncio.Task[None]], stop: asyncio.Event) -> None:
    """Wait for ``stop`` or for any task to end; then stop everything gracefully."""
    stop_waiter = asyncio.create_task(stop.wait(), name="stop")
    done, _ = await asyncio.wait([*tasks, stop_waiter], return_when=asyncio.FIRST_COMPLETED)
    stop.set()
    failure = next(
        (t.exception() for t in done if t is not stop_waiter and not t.cancelled() and t.exception()), None
    )
    for task in tasks:
        if not task.done() and task.get_name() != "dispatcher":
            task.cancel()  # Procrastinate workers shut down gracefully on cancellation
    results = await asyncio.gather(*tasks, return_exceptions=True)
    stop_waiter.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await stop_waiter
    if failure is not None:
        raise failure
    for result in results:
        if isinstance(result, Exception):
            raise result


def _install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    if sys.platform == "win32":
        # No add_signal_handler on Windows. Ctrl+C still ends asyncio.run with KeyboardInterrupt;
        # CTRL_BREAK_EVENT (SIGBREAK) is the graceful stop a supervisor or test harness can send.
        signal.signal(signal.SIGBREAK, lambda _sig, _frame: loop.call_soon_threadsafe(stop.set))
        return
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
