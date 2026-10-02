"""RT-15 extension (slice 0.3): API role vs worker role on delivery infrastructure.

BACKEND_DESIGN.md §7.6 and §21.

Every check runs real SQL as the real runtime roles. The API role's limits on ``outbox``,
``event_consumptions`` and Procrastinate are grants and role-targeted policies, so they hold
whatever ``app.user_id`` is. Documented residual risk (§7.6): code that runs arbitrary SQL as the
API role can call ``set_config`` itself and then insert an event for that user; it still cannot
read, change or delete any outbox row, or reach consumptions or the job queue.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from eca.platform.db import create_engine, create_session_factory
from eca.platform.events import NewEvent
from eca.platform.ids import uuid7
from eca.platform.outbox import publish
from eca.platform.uow import UnitOfWorkFactory
from tests.conftest import RUNTIME_ROLE, WORKER_ROLE, TempDatabase
from tests.reliability.support.synthetic import SYNTHETIC, SyntheticPayload, build_registry

pytestmark = pytest.mark.db

USER_A = uuid.UUID("00000000-0000-7000-8000-0000000000aa")
USER_B = uuid.UUID("00000000-0000-7000-8000-0000000000bb")
REGISTRY = build_registry()


def _event() -> NewEvent:
    return NewEvent(
        event_type=SYNTHETIC,
        aggregate_type="test",
        aggregate_id=uuid7(),
        payload=SyntheticPayload(marker="x"),
    )


@pytest.fixture
async def factories(isolated_db: TempDatabase) -> AsyncIterator[tuple[UnitOfWorkFactory, UnitOfWorkFactory]]:
    api_engine = create_engine(isolated_db.runtime_url, pool_size=1, max_overflow=0)
    worker_engine = create_engine(isolated_db.worker_url, pool_size=1, max_overflow=0)
    try:
        yield (
            UnitOfWorkFactory(create_session_factory(api_engine)),
            UnitOfWorkFactory(create_session_factory(worker_engine)),
        )
    finally:
        await api_engine.dispose()
        await worker_engine.dispose()


def _seed_other_users(db: TempDatabase) -> None:
    with psycopg.connect(db.admin_url) as conn:
        for user in (USER_B, None):
            conn.execute(
                "INSERT INTO outbox (id, user_id, event_type, aggregate_type, aggregate_id, payload) "
                "VALUES (%s, %s, %s, 't', %s, '{\"marker\": \"seed\"}')",
                (uuid7(), user, SYNTHETIC, uuid7()),
            )


def _sqlstate(exc: BaseException) -> str | None:
    orig = getattr(exc, "orig", None)
    return getattr(orig, "sqlstate", None)


INSUFFICIENT_PRIVILEGE = "42501"

# (1) publish, then the worker sees it (8: across users, with app.user_id unset)


async def test_api_role_publishes_and_worker_processes_across_users(
    isolated_db: TempDatabase, factories: tuple[UnitOfWorkFactory, UnitOfWorkFactory]
) -> None:
    api, worker = factories
    _seed_other_users(isolated_db)
    async with api(user_id=USER_A) as uow:
        event_id = await publish(uow, _event(), registry=REGISTRY)
    async with worker(user_id=None) as uow:
        users = {r[0] for r in await uow.session.execute(text("SELECT user_id FROM outbox"))}
        updated = await uow.session.execute(text("UPDATE outbox SET attempts = attempts WHERE true"))
    assert users == {USER_A, USER_B, None}
    assert updated.rowcount == 3  # type: ignore[attr-defined]
    with psycopg.connect(isolated_db.admin_url) as conn:
        assert conn.execute("SELECT user_id FROM outbox WHERE id = %s", (event_id,)).fetchone() == (USER_A,)


# (2)-(5), (7), (10): denied whatever app.user_id is

API_DENIED = {
    "select": "SELECT count(*) FROM outbox",
    "select_own": "SELECT id FROM outbox WHERE user_id = eca_current_user_id()",
    "update": "UPDATE outbox SET attempts = 0",
    "delete": "DELETE FROM outbox",
    "insert_returning": (
        "INSERT INTO outbox (id, user_id, event_type, aggregate_type, aggregate_id, payload) "
        "VALUES (gen_random_uuid(), eca_current_user_id(), 'test.SyntheticHappened', 't', "
        "gen_random_uuid(), '{}') "
        "RETURNING id"
    ),
    "consumptions_select": "SELECT count(*) FROM event_consumptions",
    "consumptions_insert": (
        "INSERT INTO event_consumptions (event_id, handler) VALUES (gen_random_uuid(), 'h')"
    ),
    "jobs_select": "SELECT count(*) FROM procrastinate_jobs",
    "jobs_insert": "INSERT INTO procrastinate_jobs (queue_name, task_name) VALUES ('events', 'x')",
    "jobs_defer_function": (
        "SELECT procrastinate_defer_jobs_v1(ARRAY[ROW('events','x',0,NULL,NULL,'{}'::jsonb,NULL)"
        "::procrastinate_job_to_defer_v1])"
    ),
}


@pytest.mark.parametrize("user_id", [None, USER_A, USER_B], ids=["unset", "user_a", "user_b"])
@pytest.mark.parametrize("operation", sorted(API_DENIED))
async def test_api_role_is_denied_on_infrastructure_whatever_app_user_id_is(
    isolated_db: TempDatabase,
    factories: tuple[UnitOfWorkFactory, UnitOfWorkFactory],
    user_id: uuid.UUID | None,
    operation: str,
) -> None:
    api, _ = factories
    _seed_other_users(isolated_db)
    with pytest.raises(ProgrammingError) as exc_info:
        async with api(user_id=user_id) as uow:
            await uow.session.execute(text(API_DENIED[operation]))
    assert _sqlstate(exc_info.value) == INSUFFICIENT_PRIVILEGE, exc_info.value


async def test_api_role_cannot_bypass_by_setting_app_user_id_itself(
    isolated_db: TempDatabase, factories: tuple[UnitOfWorkFactory, UnitOfWorkFactory]
) -> None:
    api, _ = factories
    _seed_other_users(isolated_db)
    for statement in (
        "SELECT count(*) FROM outbox",
        "DELETE FROM outbox",
        "SELECT count(*) FROM event_consumptions",
    ):
        with pytest.raises(ProgrammingError) as exc_info:
            async with api(user_id=USER_A) as uow:
                await uow.session.execute(
                    text("SELECT set_config('app.user_id', :u, true)"), {"u": str(USER_B)}
                )
                await uow.session.execute(text(statement))
        assert _sqlstate(exc_info.value) == INSUFFICIENT_PRIVILEGE
    # A value that is not a UUID fails closed instead of matching anything.
    with pytest.raises(DBAPIError):
        async with api(user_id=None) as uow:
            await uow.session.execute(text("SELECT set_config('app.user_id', 'not-a-uuid', true)"))
            await publish(uow, _event(), registry=REGISTRY)
    with psycopg.connect(isolated_db.admin_url) as conn:
        assert conn.execute("SELECT count(*) FROM outbox").fetchone() == (2,)  # only the seeded rows


# (6) another user's or a NULL user_id


@pytest.mark.parametrize("target", [USER_B, None], ids=["other_user", "null_user"])
async def test_api_role_cannot_insert_events_for_another_user(
    isolated_db: TempDatabase,
    factories: tuple[UnitOfWorkFactory, UnitOfWorkFactory],
    target: uuid.UUID | None,
) -> None:
    api, _ = factories
    with pytest.raises(ProgrammingError) as exc_info:
        async with api(user_id=USER_A) as uow:
            await uow.session.execute(
                text(
                    "INSERT INTO outbox (id, user_id, event_type, aggregate_type, aggregate_id, payload) "
                    "VALUES (:id, :u, 'test.SyntheticHappened', 't', :a, '{}')"
                ),
                {"id": uuid7(), "u": target, "a": uuid7()},
            )
    assert "row-level security" in str(exc_info.value).lower()
    with pytest.raises(ProgrammingError):  # publish in a unit of work without a user (NULL user_id)
        async with api(user_id=None) as uow:
            await publish(uow, _event(), registry=REGISTRY)
    with psycopg.connect(isolated_db.admin_url) as conn:
        assert conn.execute("SELECT count(*) FROM outbox").fetchone() == (0,)


# (9) the roles cannot be confused


async def test_api_role_cannot_become_the_worker_role(
    isolated_db: TempDatabase, factories: tuple[UnitOfWorkFactory, UnitOfWorkFactory]
) -> None:
    api, worker = factories
    with pytest.raises(ProgrammingError) as exc_info:
        async with api(user_id=None) as uow:
            await uow.session.execute(text(f"SET LOCAL ROLE {WORKER_ROLE}"))
    assert _sqlstate(exc_info.value) == INSUFFICIENT_PRIVILEGE
    async with api(user_id=None) as uow:
        assert (await uow.session.execute(text("SELECT current_user"))).scalar() == RUNTIME_ROLE
    async with worker(user_id=None) as uow:
        assert (await uow.session.execute(text("SELECT current_user"))).scalar() == WORKER_ROLE


async def test_worker_role_sees_no_business_rows_without_a_user(isolated_db: TempDatabase) -> None:
    """Business tables stay per-user for the worker role too (no cross-user policy)."""
    with psycopg.connect(isolated_db.admin_url, autocommit=True) as conn:
        conn.execute("CREATE TABLE rls_probe_worker (id uuid PRIMARY KEY, user_id uuid NOT NULL)")
        conn.execute("ALTER TABLE rls_probe_worker ENABLE ROW LEVEL SECURITY")
        conn.execute("ALTER TABLE rls_probe_worker FORCE ROW LEVEL SECURITY")
        conn.execute(
            "CREATE POLICY rls_probe_worker_user_isolation ON rls_probe_worker "
            "USING (user_id = eca_current_user_id()) WITH CHECK (user_id = eca_current_user_id())"
        )
        conn.execute(
            "INSERT INTO rls_probe_worker VALUES (%s, %s), (%s, %s)", (uuid7(), USER_A, uuid7(), USER_B)
        )
    engine = create_engine(isolated_db.worker_url, pool_size=1, max_overflow=0)
    try:
        factory = UnitOfWorkFactory(create_session_factory(engine))
        async with factory(user_id=None) as uow:
            assert (await uow.session.execute(text("SELECT count(*) FROM rls_probe_worker"))).scalar() == 0
        async with factory(user_id=USER_A) as uow:
            assert (await uow.session.execute(text("SELECT count(*) FROM rls_probe_worker"))).scalar() == 1
    finally:
        await engine.dispose()
