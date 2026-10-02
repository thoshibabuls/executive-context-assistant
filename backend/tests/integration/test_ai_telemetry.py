"""Slice 0.4 on PostgreSQL: ai_calls meter, cost roll-ups, RLS and grants, periodic task.

BACKEND_DESIGN.md §5.5 (own-transaction meter, 15-minute roll-ups), §7.6 (API: INSERT-only on
``ai_calls``, SELECT of its own roll-ups; worker: DML on both).
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine
from structlog.testing import capture_logs

from eca.intelligence import (
    AIClient,
    CallRecord,
    CallStatus,
    Meter,
    ProviderUnavailable,
    Usage,
    load_ai_config,
    periodic_tasks,
    rollup_costs,
)
from eca.intelligence.provider.types import EmbedRequest, EmbedResponse, GenerateRequest, GenerateResponse
from eca.platform.db import create_engine, create_session_factory
from eca.platform.jobs import create_job_app, register_periodic_tasks
from eca.platform.uow import UnitOfWorkFactory
from tests.conftest import TempDatabase

pytestmark = pytest.mark.db

USER_A = uuid.UUID("00000000-0000-7000-8000-0000000000a1")
USER_B = uuid.UUID("00000000-0000-7000-8000-0000000000b1")
T0 = datetime.datetime(2026, 10, 2, 9, 0, tzinfo=datetime.UTC)


@dataclass
class Telemetry:
    db: TempDatabase
    api: UnitOfWorkFactory
    worker: UnitOfWorkFactory
    engines: tuple[AsyncEngine, AsyncEngine]

    def rows(self, query: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        with psycopg.connect(self.db.admin_url) as conn:
            return conn.execute(query, params).fetchall()  # type: ignore[arg-type]

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> None:
        with psycopg.connect(self.db.admin_url) as conn:
            conn.execute(query, params)  # type: ignore[arg-type]


@pytest.fixture
async def tm(isolated_db: TempDatabase) -> AsyncIterator[Telemetry]:
    api_engine = create_engine(isolated_db.runtime_url, pool_size=2, max_overflow=0)
    worker_engine = create_engine(isolated_db.worker_url, pool_size=2, max_overflow=0)
    yield Telemetry(
        db=isolated_db,
        api=UnitOfWorkFactory(create_session_factory(api_engine)),
        worker=UnitOfWorkFactory(create_session_factory(worker_engine)),
        engines=(api_engine, worker_engine),
    )
    await api_engine.dispose()
    await worker_engine.dispose()


def _record(user_id: uuid.UUID | None, **kw: Any) -> CallRecord:
    base: dict[str, Any] = {
        "user_id": user_id,
        "role": "plan_query",
        "inventory_id": "AI-05",
        "model": "gemini-3.1-flash-lite",
        "prompt_version": "p1",
        "schema_version": "s1",
        "usage": Usage(input_tokens=1000, output_tokens=200, thinking_tokens=50),
        "latency_ms": 420,
        "est_cost_usd": Decimal("0.00062500"),
        "status": CallStatus.OK,
        "attempt": 1,
        "is_fallback": False,
    }
    base.update(kw)
    return CallRecord(**base)


def _insert_call(
    tm: Telemetry,
    created_at: datetime.datetime,
    *,
    user_id: uuid.UUID | None,
    role: str = "plan_query",
    **kw: Any,
) -> None:
    cols = {
        "id": uuid.uuid4(),
        "user_id": user_id,
        "role": role,
        "inventory_id": "AI-05",
        "model": "gemini-3.1-flash-lite",
        "input_tokens": 100,
        "cached_input_tokens": 0,
        "output_tokens": 10,
        "thinking_tokens": 0,
        "audio_seconds": Decimal("0"),
        "latency_ms": 100,
        "est_cost_usd": Decimal("0.00010000"),
        "status": "ok",
        "attempt": 1,
        "is_fallback": False,
        "created_at": created_at,
    }
    cols.update(kw)
    names = ", ".join(cols)
    marks = ", ".join(["%s"] * len(cols))
    tm.execute(f"INSERT INTO ai_calls ({names}) VALUES ({marks})", tuple(cols.values()))


# --- meter ----------------------------------------------------------------------------------------


async def test_api_role_meters_its_own_calls_but_cannot_read_them(tm: Telemetry) -> None:
    call_id = await Meter(tm.api).record(_record(USER_A))
    assert call_id is not None
    rows = tm.rows(
        "SELECT user_id, role, input_tokens, est_cost_usd, status FROM ai_calls WHERE id = %s", (call_id,)
    )
    assert rows == [(USER_A, "plan_query", 1000, Decimal("0.00062500"), "ok")]
    async with tm.api(user_id=USER_A) as uow:
        with pytest.raises(ProgrammingError, match="permission denied"):
            await uow.session.execute(text("SELECT count(*) FROM ai_calls"))


async def test_api_role_cannot_meter_for_another_user(tm: Telemetry) -> None:
    meter = Meter(tm.api)
    with capture_logs() as logs:
        # app.user_id is set from the record, so a mismatch needs a forged context: write as A, row says B
        async with tm.api(user_id=USER_A) as uow:
            with pytest.raises(ProgrammingError, match="row-level security"):
                await uow.session.execute(
                    text(
                        "INSERT INTO ai_calls (id, user_id, role, inventory_id, model, status) "
                        "VALUES (:id, :u, 'plan_query', 'AI-05', 'm', 'ok')"
                    ),
                    {"id": uuid.uuid4(), "u": USER_B},
                )
        # a call without a user cannot be metered by the API role (interactive calls always have one)
        assert await meter.record(_record(None)) is None
    assert any(e["event"] == "ai_call_meter_failed" for e in logs)
    assert tm.rows("SELECT count(*) FROM ai_calls") == [(0,)]


async def test_api_role_cannot_change_or_delete_meter_rows(tm: Telemetry) -> None:
    await Meter(tm.api).record(_record(USER_A))
    for statement in ("UPDATE ai_calls SET status = 'ok'", "DELETE FROM ai_calls", "TRUNCATE ai_calls"):
        async with tm.api(user_id=USER_A) as uow:
            with pytest.raises(ProgrammingError, match="permission denied"):
                await uow.session.execute(text(statement))
    assert tm.rows("SELECT count(*) FROM ai_calls") == [(1,)]


async def test_worker_role_meters_background_calls_without_a_user(tm: Telemetry) -> None:
    call_id = await Meter(tm.worker).record(
        _record(None, status=CallStatus.TIMEOUT, error_code="ProviderTimeout", attempt=2, is_fallback=True)
    )
    assert tm.rows(
        "SELECT user_id, status, error_code, attempt, is_fallback FROM ai_calls WHERE id = %s", (call_id,)
    ) == [(None, "timeout", "ProviderTimeout", 2, True)]


async def test_meter_row_survives_the_callers_rollback(tm: Telemetry) -> None:
    meter = Meter(tm.worker)
    with pytest.raises(RuntimeError, match="caller failed"):
        async with tm.worker(user_id=None) as uow:
            await uow.session.execute(text("SELECT 1"))
            assert await meter.record(_record(USER_A)) is not None
            raise RuntimeError("caller failed")
    assert tm.rows("SELECT count(*) FROM ai_calls") == [(1,)]


async def test_meter_rows_hold_no_content(tm: Telemetry) -> None:
    columns = {
        r[0]
        for r in tm.rows("SELECT column_name FROM information_schema.columns WHERE table_name = 'ai_calls'")
    }
    assert not columns & {"prompt", "output", "text", "content", "response", "input"}


class _Answer(BaseModel):
    answer: str


class _Provider:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    async def generate(self, request: GenerateRequest) -> GenerateResponse:
        if self.fail:
            raise ProviderUnavailable("down", code=503)
        return GenerateResponse(text='{"answer": "ok"}', usage=Usage(input_tokens=2000, output_tokens=100))

    async def embed(self, request: EmbedRequest) -> EmbedResponse:
        return EmbedResponse(vectors=((0.1, 0.2),), usage=Usage(input_tokens=3))


async def test_client_meters_success_and_failure_through_the_database(tm: Telemetry) -> None:
    config = load_ai_config()
    for fail in (False, True):
        client = AIClient(
            registry=config.registry,
            prices=config.prices,
            mode="live",
            provider=_Provider(fail),
            meter=Meter(tm.api),
        )
        call = client.generate(
            "plan_query",
            prompt_version="p1",
            schema_version="s1",
            output_model=_Answer,
            contents=["synthetic"],
            user_id=USER_A,
        )
        if fail:
            with pytest.raises(ProviderUnavailable):
                await call
        else:
            result = await call
            assert result.call_id is not None
    rows = tm.rows(
        "SELECT status, input_tokens, est_cost_usd > 0, error_code FROM ai_calls ORDER BY created_at, id"
    )
    assert sorted(rows, key=str) == sorted(
        [("ok", 2000, True, None), ("provider_error", 0, False, "ProviderUnavailable")], key=str
    )


# --- roll-ups -------------------------------------------------------------------------------------


def _rollups(tm: Telemetry) -> list[tuple[Any, ...]]:
    return tm.rows(
        "SELECT bucket_start, user_id, role, calls, failed_calls, retry_calls, fallback_calls, input_tokens, "
        "est_cost_usd FROM ai_cost_rollups ORDER BY bucket_start, user_id NULLS FIRST, role"
    )


async def test_rollup_groups_by_bucket_user_role_and_is_idempotent(tm: Telemetry) -> None:
    _insert_call(tm, T0 + datetime.timedelta(minutes=1), user_id=USER_A)
    _insert_call(tm, T0 + datetime.timedelta(minutes=14), user_id=USER_A, status="timeout", attempt=2)
    _insert_call(tm, T0 + datetime.timedelta(minutes=16), user_id=USER_A, is_fallback=True)
    _insert_call(tm, T0 + datetime.timedelta(minutes=2), user_id=USER_B, role="answer_lookup")
    _insert_call(tm, T0 + datetime.timedelta(minutes=3), user_id=None, role="email_extract")
    _insert_call(tm, T0 + datetime.timedelta(minutes=4), user_id=None, role="email_extract")
    now = T0 + datetime.timedelta(minutes=20)
    first = await rollup_costs(tm.worker, now=now)
    snapshot = _rollups(tm)
    assert first == 4
    b0, b1 = T0, T0 + datetime.timedelta(minutes=15)
    cost = Decimal("0.00010000")
    assert [r[:8] for r in snapshot] == [
        (b0, None, "email_extract", 2, 0, 0, 0, 200),
        (b0, USER_A, "plan_query", 2, 1, 1, 0, 200),
        (b0, USER_B, "answer_lookup", 1, 0, 0, 0, 100),
        (b1, USER_A, "plan_query", 1, 0, 0, 1, 100),
    ]
    assert snapshot[0][8] == 2 * cost
    assert await rollup_costs(tm.worker, now=now) == first
    assert _rollups(tm) == snapshot

    _insert_call(tm, T0 + datetime.timedelta(minutes=17), user_id=USER_A)
    await rollup_costs(tm.worker, now=now)
    assert [r[3] for r in _rollups(tm) if r[0] == b1] == [2]


async def test_rollup_leaves_buckets_outside_the_window(tm: Telemetry) -> None:
    _insert_call(tm, T0, user_id=USER_A)
    await rollup_costs(tm.worker, now=T0 + datetime.timedelta(minutes=5))
    _insert_call(tm, T0 + datetime.timedelta(minutes=1), user_id=USER_A)  # late row in an old bucket
    await rollup_costs(tm.worker, now=T0 + datetime.timedelta(hours=3))
    assert [r[3] for r in _rollups(tm)] == [1], "buckets older than the window are not recomputed"


async def test_rollup_unique_per_bucket_with_null_user(tm: Telemetry) -> None:
    _insert_call(tm, T0, user_id=None)
    await rollup_costs(tm.worker, now=T0)
    with pytest.raises(psycopg.errors.UniqueViolation):
        tm.execute(
            "INSERT INTO ai_cost_rollups (bucket_start, user_id, role, model, calls, failed_calls, "
            "retry_calls, fallback_calls, input_tokens, cached_input_tokens, output_tokens, "
            "thinking_tokens, audio_seconds, latency_ms_total, est_cost_usd) "
            "VALUES (%s, NULL, 'plan_query', 'gemini-3.1-flash-lite', 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)",
            (T0,),
        )


async def test_api_role_reads_only_its_own_rollups_and_cannot_write_them(tm: Telemetry) -> None:
    _insert_call(tm, T0, user_id=USER_A)
    _insert_call(tm, T0, user_id=USER_B)
    _insert_call(tm, T0, user_id=None)
    await rollup_costs(tm.worker, now=T0)
    async with tm.api(user_id=USER_A) as uow:
        users = (await uow.session.execute(text("SELECT user_id FROM ai_cost_rollups"))).scalars().all()
    assert users == [USER_A]
    async with tm.api(user_id=None) as uow:
        assert (await uow.session.execute(text("SELECT count(*) FROM ai_cost_rollups"))).scalar_one() == 0
    for statement in ("DELETE FROM ai_cost_rollups", "UPDATE ai_cost_rollups SET calls = 0"):
        async with tm.api(user_id=USER_A) as uow:
            with pytest.raises(ProgrammingError, match="permission denied"):
                await uow.session.execute(text(statement))


async def test_api_role_cannot_run_the_rollup(tm: Telemetry) -> None:
    _insert_call(tm, T0, user_id=USER_A)
    with pytest.raises(ProgrammingError, match="permission denied"):
        await rollup_costs(tm.api, now=T0)
    assert tm.rows("SELECT count(*) FROM ai_cost_rollups") == [(0,)]


# --- periodic task --------------------------------------------------------------------------------


async def test_cost_rollup_is_registered_as_a_locked_periodic_task(tm: Telemetry) -> None:
    app = create_job_app(tm.db.worker_url, pool_max_size=2)
    specs = periodic_tasks()
    register_periodic_tasks(app, specs, tm.worker)
    task = app.tasks["eca.intelligence.cost_rollup"]
    assert task.queue == "schedule" and task.lock == "cost_rollup" and task.queueing_lock == "cost_rollup"
    periodic = [p for p in app.periodic_registry.periodic_tasks.values() if p.task is task]
    assert len(periodic) == 1 and periodic[0].cron == "*/15 * * * *"

    _insert_call(tm, T0, user_id=USER_A)
    await task.func(timestamp=int((T0 + datetime.timedelta(minutes=10)).timestamp()))
    assert [r[3] for r in _rollups(tm)] == [1]
