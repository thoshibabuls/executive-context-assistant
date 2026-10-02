"""RT-15: row-level security with ``app.user_id`` unset, wrong, and correct (BACKEND_DESIGN.md §21).

The probe table is created with the same ``user_isolation_ddl`` helper that every user-owned
table will use. Rows are inserted by the owner role; reads and writes go through the real
``UnitOfWork`` connected as the non-superuser runtime role, and the queries contain **no**
``WHERE user_id`` filter, so any isolation observed is enforced by PostgreSQL, not the app.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from eca.platform.db import create_engine, create_session_factory
from eca.platform.rls import drop_user_isolation_ddl, user_isolation_ddl
from eca.platform.uow import UnitOfWork, UnitOfWorkFactory
from tests.conftest import RUNTIME_ROLE, TempDatabase

pytestmark = pytest.mark.db

USER_A = uuid.UUID("00000000-0000-7000-8000-00000000000a")
USER_B = uuid.UUID("00000000-0000-7000-8000-00000000000b")
USER_C = uuid.UUID("00000000-0000-7000-8000-00000000000c")  # owns nothing


@pytest.fixture(scope="module")
def probe_table(migrated_db: TempDatabase) -> Iterator[None]:
    with psycopg.connect(migrated_db.admin_url, autocommit=True) as conn:
        conn.execute(
            "CREATE TABLE rls_probe (id uuid PRIMARY KEY, user_id uuid NOT NULL, payload text NOT NULL)"
        )
        for stmt in user_isolation_ddl("rls_probe"):
            conn.execute(stmt)
        conn.execute(
            "INSERT INTO rls_probe (id, user_id, payload) VALUES (%s,%s,'a1'),(%s,%s,'a2'),(%s,%s,'b1')",
            (uuid.uuid4(), USER_A, uuid.uuid4(), USER_A, uuid.uuid4(), USER_B),
        )
    yield
    with psycopg.connect(migrated_db.admin_url, autocommit=True) as conn:
        for stmt in drop_user_isolation_ddl("rls_probe"):
            conn.execute(stmt)
        conn.execute("DROP TABLE rls_probe")


@pytest.fixture
async def uow_factory(migrated_db: TempDatabase, probe_table: None) -> AsyncIterator[UnitOfWorkFactory]:
    # pool_size=1, max_overflow=0: every unit of work reuses the same physical connection,
    # which is what makes the leak checks below meaningful.
    engine = create_engine(migrated_db.runtime_url, pool_size=1, max_overflow=0)
    try:
        yield UnitOfWorkFactory(create_session_factory(engine))
    finally:
        await engine.dispose()


async def _payloads(uow: UnitOfWork) -> list[str]:
    rows = await uow.session.execute(text("SELECT payload FROM rls_probe ORDER BY payload"))
    return [r[0] for r in rows]


def test_runtime_role_cannot_bypass_rls(migrated_db: TempDatabase, probe_table: None) -> None:
    with psycopg.connect(migrated_db.admin_url) as conn:
        row = conn.execute(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = %s", (RUNTIME_ROLE,)
        ).fetchone()
        forced = conn.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'rls_probe'"
        ).fetchone()
    assert row == (False, False)
    assert forced == (True, True)


async def test_no_user_set_returns_zero_rows(uow_factory: UnitOfWorkFactory) -> None:
    async with uow_factory(user_id=None) as uow:
        assert await _payloads(uow) == []
        assert (await uow.session.execute(text("SELECT count(*) FROM rls_probe"))).scalar() == 0


async def test_wrong_user_returns_zero_rows(uow_factory: UnitOfWorkFactory) -> None:
    async with uow_factory(user_id=USER_C) as uow:
        assert await _payloads(uow) == []
    async with uow_factory(user_id=USER_B) as uow:  # B asks for A's rows by payload
        rows = await uow.session.execute(text("SELECT payload FROM rls_probe WHERE payload IN ('a1','a2')"))
        assert rows.all() == []


async def test_correct_user_sees_only_own_rows(uow_factory: UnitOfWorkFactory) -> None:
    async with uow_factory(user_id=USER_A) as uow:
        assert await _payloads(uow) == ["a1", "a2"]
    async with uow_factory(user_id=USER_B) as uow:
        assert await _payloads(uow) == ["b1"]


async def test_writes_for_another_user_are_rejected(uow_factory: UnitOfWorkFactory) -> None:
    with pytest.raises(DBAPIError) as exc_info:
        async with uow_factory(user_id=USER_A) as uow:
            await uow.session.execute(
                text("INSERT INTO rls_probe (id, user_id, payload) VALUES (:id, :uid, 'forged')"),
                {"id": uuid.uuid4(), "uid": USER_B},
            )
    assert "row-level security" in str(exc_info.value).lower()

    async with uow_factory(user_id=USER_A) as uow:
        updated = await uow.session.execute(
            text("UPDATE rls_probe SET payload = 'hijack' WHERE payload = 'b1'")
        )
        assert updated.rowcount == 0  # type: ignore[attr-defined]
        deleted = await uow.session.execute(text("DELETE FROM rls_probe WHERE payload = 'b1'"))
        assert deleted.rowcount == 0  # type: ignore[attr-defined]
    async with uow_factory(user_id=USER_B) as uow:
        assert await _payloads(uow) == ["b1"]


async def test_user_context_does_not_leak_across_pooled_transactions(uow_factory: UnitOfWorkFactory) -> None:
    async with uow_factory(user_id=USER_A) as uow:
        assert await _payloads(uow) == ["a1", "a2"]
    async with uow_factory(user_id=None) as uow:  # same physical connection (pool_size=1)
        setting = (await uow.session.execute(text("SELECT current_setting('app.user_id', true)"))).scalar()
        assert setting in (None, "")
        assert await _payloads(uow) == []


async def test_rolled_back_unit_of_work_leaves_no_context(uow_factory: UnitOfWorkFactory) -> None:
    with pytest.raises(RuntimeError):
        async with uow_factory(user_id=USER_A) as uow:
            assert await _payloads(uow) == ["a1", "a2"]
            raise RuntimeError("fail inside the unit of work")
    async with uow_factory(user_id=None) as uow:
        assert await _payloads(uow) == []
