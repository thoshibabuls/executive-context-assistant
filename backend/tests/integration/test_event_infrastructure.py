"""Outbox, dispatcher, handler wrapper, reconciler and Procrastinate integration on PostgreSQL.

In-process tests with real roles: the API role publishes, the worker role dispatches and handles.
Subprocess crash tests (RT-01, RT-05) live in ``tests/reliability``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import procrastinate
import psycopg
import pytest
from pydantic import BaseModel
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine
from structlog.testing import capture_logs

from eca.platform.config import Settings
from eca.platform.db import create_engine, create_session_factory
from eca.platform.dispatch import Dispatcher, DispatchPolicy, reconcile_once
from eca.platform.events import EventRegistry, HandlerContext, NewEvent, UnregisteredEventType
from eca.platform.handlers import run_handler
from eca.platform.ids import uuid7
from eca.platform.jobs import (
    HandlerRetryStrategy,
    create_job_app,
    job_lock_key,
    recover_stalled_jobs,
    register_handler_tasks,
)
from eca.platform.outbox import publish, retry_failed
from eca.platform.uow import UnitOfWorkFactory
from eca.worker import WorkerConfig, WorkerStartupError, run_worker
from eca.worker.cli import outbox_retry
from tests.conftest import RUNTIME_ROLE, WORKER_ROLE, TempDatabase
from tests.reliability.support.synthetic import (
    ALWAYS_FAILS,
    FANOUT,
    NO_HANDLERS,
    SYNTHETIC,
    SyntheticPayload,
    build_registry,
    create_rt_tables,
)

pytestmark = pytest.mark.db

USER_A = uuid.UUID("00000000-0000-7000-8000-0000000000a1")
USER_B = uuid.UUID("00000000-0000-7000-8000-0000000000b1")


@dataclass
class Infra:
    db: TempDatabase
    registry: EventRegistry
    api: UnitOfWorkFactory
    worker: UnitOfWorkFactory
    api_engine: AsyncEngine
    job_app: procrastinate.App

    def dispatcher(self, **policy: Any) -> Dispatcher:
        return Dispatcher(
            self.worker, self.registry, self.job_app, DispatchPolicy(**policy), rand=lambda: 0.5
        )

    def rows(self, query: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        with psycopg.connect(self.db.admin_url) as conn:
            return conn.execute(query, params).fetchall()  # type: ignore[arg-type]

    def scalar(self, query: str, params: tuple[Any, ...] = ()) -> Any:
        return self.rows(query, params)[0][0]


def _conninfo(url: str) -> str:
    return url  # tests use plain postgresql:// URLs


@pytest.fixture
async def infra(isolated_db: TempDatabase) -> AsyncIterator[Infra]:
    create_rt_tables(isolated_db.admin_url, api_role=RUNTIME_ROLE, worker_role=WORKER_ROLE)
    registry = build_registry()
    api_engine = create_engine(isolated_db.runtime_url, pool_size=2, max_overflow=0)
    worker_engine = create_engine(isolated_db.worker_url, pool_size=4, max_overflow=0)
    worker = UnitOfWorkFactory(create_session_factory(worker_engine))
    job_app = create_job_app(_conninfo(isolated_db.worker_url), pool_max_size=5)
    register_handler_tasks(job_app, registry, worker, HandlerRetryStrategy(base_s=0.01, max_delay_s=0.05))
    async with job_app.open_async():
        yield Infra(
            db=isolated_db,
            registry=registry,
            api=UnitOfWorkFactory(create_session_factory(api_engine)),
            worker=worker,
            api_engine=api_engine,
            job_app=job_app,
        )
    await api_engine.dispose()
    await worker_engine.dispose()


def _event(event_type: str = SYNTHETIC, marker: str = "m") -> NewEvent:
    return NewEvent(
        event_type=event_type,
        aggregate_type="test",
        aggregate_id=uuid7(),
        payload=SyntheticPayload(marker=marker),
    )


async def _publish(
    infra: Infra, event_type: str = SYNTHETIC, *, user_id: uuid.UUID = USER_A, n: int = 1
) -> list[uuid.UUID]:
    async with infra.api(user_id=user_id) as uow:
        return [await publish(uow, _event(event_type, f"m{i}"), registry=infra.registry) for i in range(n)]


# ---------------------------------------------------------------- publish


async def test_publish_commits_together_with_the_business_change(infra: Infra) -> None:
    async with infra.api(user_id=USER_A) as uow:
        await uow.session.execute(
            text("INSERT INTO rt_business (id, user_id, note) VALUES (:id, :u, 'committed')"),
            {"id": uuid7(), "u": USER_A},
        )
        event_id = await publish(uow, _event(), registry=infra.registry)
    assert infra.rows("SELECT note FROM rt_business") == [("committed",)]
    row = infra.rows(
        "SELECT user_id, status, attempts, event_type, payload FROM outbox WHERE id = %s", (event_id,)
    )
    assert row == [(USER_A, "pending", 0, SYNTHETIC, {"marker": "m"})]


async def test_publish_rolls_back_together_with_the_business_change(infra: Infra) -> None:
    with pytest.raises(RuntimeError, match="business failure"):
        async with infra.api(user_id=USER_A) as uow:
            await uow.session.execute(
                text("INSERT INTO rt_business (id, user_id, note) VALUES (:id, :u, 'rolled back')"),
                {"id": uuid7(), "u": USER_A},
            )
            await publish(uow, _event(), registry=infra.registry)
            raise RuntimeError("business failure")
    assert infra.scalar("SELECT count(*) FROM rt_business") == 0
    assert infra.scalar("SELECT count(*) FROM outbox") == 0


async def test_publish_uses_the_callers_transaction_and_no_returning(infra: Infra) -> None:
    statements: list[str] = []
    connections: set[int] = set()

    def capture(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        statements.append(statement)
        connections.add(id(conn.connection.dbapi_connection))

    event.listen(infra.api_engine.sync_engine, "before_cursor_execute", capture)
    try:
        async with infra.api(user_id=USER_A) as uow:
            await publish(uow, _event(), registry=infra.registry)
    finally:
        event.remove(infra.api_engine.sync_engine, "before_cursor_execute", capture)
    inserts = [s for s in statements if s.lstrip().upper().startswith("INSERT INTO OUTBOX")]
    assert len(inserts) == 1
    assert "RETURNING" not in inserts[0].upper()
    assert "ON CONFLICT" not in inserts[0].upper()
    assert len(connections) == 1  # set_config + insert on one connection, one transaction


async def test_publish_takes_user_id_from_the_unit_of_work(infra: Infra) -> None:
    [api_event] = await _publish(infra, user_id=USER_B)
    async with infra.worker(user_id=None) as uow:  # a system unit of work publishes a system event
        system_event = await publish(uow, _event(), registry=infra.registry)
    users = dict(infra.rows("SELECT id, user_id FROM outbox"))
    assert users == {api_event: USER_B, system_event: None}


async def test_publish_rejects_unregistered_types_and_wrong_payloads(infra: Infra) -> None:
    class Other(BaseModel):
        marker: str

    async with infra.api(user_id=USER_A) as uow:
        with pytest.raises(UnregisteredEventType):
            await publish(uow, _event("test.NeverRegistered"), registry=infra.registry)
        with pytest.raises(TypeError):
            await publish(
                uow,
                NewEvent(
                    event_type=SYNTHETIC, aggregate_type="t", aggregate_id=uuid7(), payload=Other(marker="x")
                ),
                registry=infra.registry,
            )
    assert infra.scalar("SELECT count(*) FROM outbox") == 0


# ---------------------------------------------------------------- dispatcher


def _jobs(infra: Infra) -> list[tuple[Any, ...]]:
    return infra.rows(
        "SELECT task_name, queue_name, lock, queueing_lock, args->'envelope'->>'id', status "
        "FROM procrastinate_jobs ORDER BY task_name, id"
    )


async def test_dispatch_defers_one_job_per_handler_with_lock_keys(infra: Infra) -> None:
    [event_id] = await _publish(infra, FANOUT)
    result = await infra.dispatcher().dispatch_once()
    assert (result.claimed, result.dispatched, result.errors) == (1, 1, 0)
    assert _jobs(infra) == [
        (
            f"eca.handler.{h}",
            "events",
            job_lock_key(h, event_id),
            job_lock_key(h, event_id),
            str(event_id),
            "todo",
        )
        for h in ("test_fanout_a", "test_fanout_b")
    ]
    status, dispatched_at = infra.rows("SELECT status, dispatched_at FROM outbox WHERE id = %s", (event_id,))[
        0
    ]
    assert status == "dispatched" and dispatched_at is not None


async def test_event_with_zero_handlers_is_dispatched_without_jobs(infra: Infra) -> None:
    await _publish(infra, NO_HANDLERS)
    assert (await infra.dispatcher().dispatch_once()).dispatched == 1
    assert infra.scalar("SELECT status FROM outbox") == "dispatched"
    assert _jobs(infra) == []


async def test_unregistered_event_type_backs_off_and_fails_after_ten_attempts(infra: Infra) -> None:
    event_id = uuid7()
    infra.rows(
        "INSERT INTO outbox (id, user_id, event_type, aggregate_type, aggregate_id, payload) "
        "VALUES (%s, %s, 'test.Unknown', 't', %s, '{}') RETURNING id",
        (event_id, USER_A, uuid7()),
    )
    dispatcher = infra.dispatcher()
    for n in range(1, 11):
        infra.rows("UPDATE outbox SET next_attempt_at = now() WHERE id = %s RETURNING id", (event_id,))
        result = await dispatcher.dispatch_once()
        assert (result.claimed, result.errors) == (1, 1)
        status, attempts, last_error, delay = infra.rows(
            "SELECT status, attempts, last_error, extract(epoch FROM next_attempt_at - now()) "
            "FROM outbox WHERE id = %s",
            (event_id,),
        )[0]
        assert attempts == n
        assert last_error == "unregistered_event_type:UnregisteredEventType"
        assert status == ("failed" if n == 10 else "pending")
        expected = min(2 ** (n - 1) + 0.5, 300.0)  # rand fixed at 0.5
        assert expected - 1.0 < float(delay) <= expected
    infra.rows("UPDATE outbox SET next_attempt_at = now() WHERE id = %s RETURNING id", (event_id,))
    assert (await dispatcher.dispatch_once()).claimed == 0  # failed rows are never claimed
    assert _jobs(infra) == []


async def test_dispatcher_skips_rows_locked_by_another_dispatcher(infra: Infra) -> None:
    locked, free = await _publish(infra, n=2)
    with psycopg.connect(infra.db.admin_url) as holder:  # simulates a dispatcher mid-batch
        holder.execute("SELECT id FROM outbox WHERE id = %s FOR UPDATE", (locked,))
        result = await infra.dispatcher().dispatch_once()
        assert (result.claimed, result.dispatched) == (1, 1)
        assert infra.scalar("SELECT status FROM outbox WHERE id = %s", (free,)) == "dispatched"
        assert infra.scalar("SELECT status FROM outbox WHERE id = %s", (locked,)) == "pending"
    assert (await infra.dispatcher().dispatch_once()).dispatched == 1
    assert infra.scalar("SELECT count(*) FROM outbox WHERE status = 'dispatched'") == 2


async def test_concurrent_dispatchers_defer_each_event_once_per_handler(infra: Infra) -> None:
    await _publish(infra, FANOUT, user_id=USER_A, n=20)
    await _publish(infra, FANOUT, user_id=USER_B, n=20)
    one, two = infra.dispatcher(batch_size=3), infra.dispatcher(batch_size=3)

    async def drain(d: Dispatcher) -> int:
        total = 0
        while (r := await d.dispatch_once()).claimed:
            total += r.dispatched
        return total

    with capture_logs() as logs:
        counts = await asyncio.gather(drain(one), drain(two))
    assert sum(counts) == 40
    assert len([e for e in logs if e["event"] == "job_deferred"]) == 80
    assert [e for e in logs if e["event"] == "job_already_enqueued"] == []
    assert infra.scalar("SELECT count(DISTINCT queueing_lock) FROM procrastinate_jobs") == 80
    assert infra.scalar("SELECT count(*) FROM procrastinate_jobs") == 80


async def test_redispatch_of_a_still_queued_event_is_absorbed_by_the_queueing_lock(infra: Infra) -> None:
    [event_id] = await _publish(infra)
    await infra.dispatcher().dispatch_once()
    infra.rows("UPDATE outbox SET status = 'pending' WHERE id = %s RETURNING id", (event_id,))  # lost mark
    with capture_logs() as logs:
        assert (await infra.dispatcher().dispatch_once()).dispatched == 1
    assert [e["event"] for e in logs if e.get("handler") == "test_effect"] == ["job_already_enqueued"]
    assert infra.scalar("SELECT count(*) FROM procrastinate_jobs") == 1


# ---------------------------------------------------------------- reconciler and operator retry


async def test_reconciler_redispatches_only_old_pending_rows(infra: Infra) -> None:
    old, new = await _publish(infra, n=2)
    infra.rows(
        "UPDATE outbox SET created_at = now() - interval '2 minutes' WHERE id = %s RETURNING id", (old,)
    )
    failed = uuid7()
    infra.rows(
        "INSERT INTO outbox (id, event_type, aggregate_type, aggregate_id, payload, status, attempts, "
        "created_at) VALUES (%s, %s, 't', %s, '{\"marker\": \"x\"}', 'failed', 10, "
        "now() - interval '1 hour') "
        "RETURNING id",
        (failed, SYNTHETIC, uuid7()),
    )
    report = await reconcile_once(infra.dispatcher(), infra.worker, min_age_s=60)
    assert report.redispatched == 1
    assert report.failed_count == 1
    assert report.oldest_pending_age_s is not None and report.oldest_pending_age_s < 60
    statuses = dict(infra.rows("SELECT id, status FROM outbox"))
    assert statuses == {old: "dispatched", new: "pending", failed: "failed"}


async def test_outbox_retry_requeues_failed_rows(infra: Infra) -> None:
    first, second = uuid7(), uuid7()
    for event_id in (first, second):
        infra.rows(
            "INSERT INTO outbox (id, event_type, aggregate_type, aggregate_id, payload, status, attempts, "
            "last_error) "
            "VALUES (%s, %s, 't', %s, '{}', 'failed', 10, 'dispatch_error:X') RETURNING id",
            (event_id, SYNTHETIC, uuid7()),
        )
    async with infra.worker(user_id=None) as uow:
        assert await retry_failed(uow, event_id=first) == 1
    assert infra.rows("SELECT status, attempts, last_error FROM outbox WHERE id = %s", (first,)) == [
        ("pending", 0, None)
    ]
    settings = Settings(_env_file=None, api_worker_database_url=infra.db.worker_url)  # type: ignore[call-arg]
    assert await outbox_retry(settings, None) == 1  # the `eca ops outbox retry` path, as the worker role
    assert infra.scalar("SELECT count(*) FROM outbox WHERE status = 'failed'") == 0


# ---------------------------------------------------------------- handler wrapper


async def _envelope(
    infra: Infra, event_type: str = SYNTHETIC, user_id: uuid.UUID | None = USER_A
) -> dict[str, Any]:
    if user_id is None:
        async with infra.worker(user_id=None) as uow:
            event_id = await publish(uow, _event(event_type), registry=infra.registry)
    else:
        [event_id] = await _publish(infra, event_type, user_id=user_id)
    await infra.dispatcher().dispatch_once()
    args = infra.scalar(
        "SELECT args FROM procrastinate_jobs WHERE args->'envelope'->>'id' = %s", (str(event_id),)
    )
    return dict(args["envelope"])


def _effects(infra: Infra) -> list[tuple[Any, ...]]:
    return infra.rows("SELECT handler, user_id, seen_app_user_id FROM rt_effects ORDER BY id")


async def test_handler_applies_once_and_skips_duplicate_delivery(infra: Infra) -> None:
    envelope = await _envelope(infra)
    assert await run_handler(infra.worker, infra.registry, "test_effect", envelope, attempt=0) is True
    assert await run_handler(infra.worker, infra.registry, "test_effect", envelope, attempt=1) is False
    assert _effects(infra) == [("test_effect", USER_A, str(USER_A))]
    assert infra.scalar("SELECT count(*) FROM event_consumptions") == 1


async def test_system_event_handler_runs_without_user_context(infra: Infra) -> None:
    envelope = await _envelope(infra, user_id=None)
    assert await run_handler(infra.worker, infra.registry, "test_effect", envelope, attempt=0)
    [(_, user_id, seen)] = _effects(infra)
    assert user_id is None and seen in (None, "")


async def test_failed_handler_leaves_no_consumption_and_can_be_retried(infra: Infra) -> None:
    calls = 0

    async def flaky(ctx: HandlerContext) -> None:
        nonlocal calls
        calls += 1
        await ctx.uow.session.execute(
            text("INSERT INTO rt_effects (event_id, handler) VALUES (:e, 'gated')"), {"e": ctx.envelope.id}
        )
        if calls == 1:
            raise RuntimeError("fails after writing its effect")

    infra.registry.handles(ALWAYS_FAILS, name="test_fail_once")(flaky)
    envelope = await _envelope(infra, ALWAYS_FAILS)
    with pytest.raises(RuntimeError):
        await run_handler(infra.worker, infra.registry, "test_fail_once", envelope, attempt=0)
    assert infra.scalar("SELECT count(*) FROM event_consumptions") == 0
    assert infra.scalar("SELECT count(*) FROM rt_effects") == 0  # the effect rolled back with it
    assert await run_handler(infra.worker, infra.registry, "test_fail_once", envelope, attempt=1)
    assert infra.scalar("SELECT count(*) FROM rt_effects") == 1
    assert infra.scalar("SELECT count(*) FROM event_consumptions WHERE handler = 'test_fail_once'") == 1


@pytest.mark.parametrize("first_outcome", ["commits", "fails"])
async def test_concurrent_duplicate_delivery_is_safe(infra: Infra, first_outcome: str) -> None:
    gate = asyncio.Event()
    entered = asyncio.Event()

    async def gated(ctx: HandlerContext) -> None:
        await ctx.uow.session.execute(
            text("INSERT INTO rt_effects (event_id, handler) VALUES (:e, 'gated')"), {"e": ctx.envelope.id}
        )
        if not entered.is_set():
            entered.set()
            await gate.wait()
            if first_outcome == "fails":
                raise RuntimeError("first delivery fails")

    infra.registry.handles(ALWAYS_FAILS, name="test_gated")(gated)
    envelope = await _envelope(infra, ALWAYS_FAILS)
    first = asyncio.create_task(run_handler(infra.worker, infra.registry, "test_gated", envelope, attempt=0))
    await entered.wait()
    second = asyncio.create_task(run_handler(infra.worker, infra.registry, "test_gated", envelope, attempt=0))

    def second_is_blocked() -> bool:
        return bool(
            infra.scalar(
                "SELECT count(*) FROM pg_stat_activity WHERE usename = %s AND wait_event_type = 'Lock' "
                "AND query LIKE 'INSERT INTO event_consumptions%%'",
                (WORKER_ROLE,),
            )
        )

    for _ in range(100):  # the duplicate waits on the first delivery's uncommitted consumption row
        if second_is_blocked():
            break
        await asyncio.sleep(0.05)
    assert second_is_blocked()
    gate.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    if first_outcome == "commits":
        assert results == [True, False]
    else:
        assert isinstance(results[0], RuntimeError) and results[1] is True
    assert infra.scalar("SELECT count(*) FROM rt_effects") == 1
    assert infra.scalar("SELECT count(*) FROM event_consumptions WHERE handler = 'test_gated'") == 1


# ---------------------------------------------------------------- Procrastinate: retries and stalled jobs


async def test_handler_job_is_dead_after_eight_runs(infra: Infra) -> None:
    [event_id] = await _publish(infra, ALWAYS_FAILS)
    await infra.dispatcher().dispatch_once()
    with capture_logs() as logs:
        for _ in range(200):
            await infra.job_app.run_worker_async(
                queues=["events"], wait=False, install_signal_handlers=False, update_heartbeat_interval=0.5
            )
            if infra.scalar("SELECT status FROM procrastinate_jobs") == "failed":
                break
            await asyncio.sleep(0.02)
    assert infra.rows("SELECT status, attempts FROM procrastinate_jobs") == [("failed", 8)]
    dead = [e for e in logs if e["event"] == "handler_job_dead"]
    assert len(dead) == 1 and dead[0]["run"] == 8 and dead[0]["event_id"] == str(event_id)
    assert infra.scalar("SELECT count(*) FROM rt_effects") == 0
    assert infra.scalar("SELECT count(*) FROM event_consumptions") == 0
    assert (
        infra.scalar("SELECT status FROM outbox") == "dispatched"
    )  # handler failures never touch the outbox
    assert infra.scalar("SELECT attempts FROM outbox") == 0


async def test_stalled_job_is_requeued_and_counts_as_an_attempt(infra: Infra) -> None:
    await _publish(infra)
    await infra.dispatcher().dispatch_once()
    manager = infra.job_app.job_manager
    worker_id = await manager.register_worker()
    job = await manager.fetch_job(queues=["events"], worker_id=worker_id)
    assert job is not None and infra.scalar("SELECT status FROM procrastinate_jobs") == "doing"
    assert await recover_stalled_jobs(infra.job_app, stalled_timeout_s=30) == 0  # heartbeat is fresh
    infra.rows(
        "UPDATE procrastinate_workers SET last_heartbeat = now() - interval '1 minute' "
        "WHERE id = %s RETURNING id",
        (worker_id,),
    )
    assert await recover_stalled_jobs(infra.job_app, stalled_timeout_s=30) == 1
    assert infra.rows("SELECT status, attempts FROM procrastinate_jobs") == [("todo", 1)]
    assert infra.scalar("SELECT count(*) FROM procrastinate_workers") == 0


# ---------------------------------------------------------------- worker startup


async def test_worker_refuses_to_run_as_the_api_role(isolated_db: TempDatabase) -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        api_env="test",
        api_worker_database_url=isolated_db.runtime_url,
        api_db_worker_role=WORKER_ROLE,
        api_db_runtime_role=RUNTIME_ROLE,
    )
    with pytest.raises(WorkerStartupError, match="worker role"):
        await run_worker(registry=build_registry(), config=WorkerConfig(), settings=settings)
