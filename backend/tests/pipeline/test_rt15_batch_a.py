"""RT-15 extended to the Batch A tables (BACKEND_DESIGN.md §7.6, §21).

Two tenants with data in every table. For each business table: with ``app.user_id`` unset both
runtime roles see zero rows; with ``app.user_id`` = A they see exactly A's rows; a write for B is
rejected. ``users``: the worker enumerates IDs, status and timezone only and can read no email.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from eca.ingestion import sync_mail
from tests.conftest import TempDatabase
from tests.pipeline.support import Pipeline, Tenant, pipeline, world_v1_messages

pytestmark = pytest.mark.db

TABLES = (
    "organizations",
    "persons",
    "person_identifiers",
    "connections",
    "sync_cursors",
    "source_items",
    "conversations",
    "messages",
    "message_participants",
    "entity_mentions",
)


async def _tenant_with_mail(p: Pipeline, email: str, name: str, count: int) -> Tenant:
    t = await p.tenant(email=email, name=name)
    msgs = world_v1_messages()[:count]
    from dataclasses import replace

    p.feed(t).extend(
        replace(m, external_id=f"{email}:{m.external_id}", rfc822_id=f"<{email}.{m.external_id}@x.example>")
        for m in msgs
    )
    await sync_mail(
        p.worker,
        p.connectors,
        user_id=t.user_id,
        connection_id=t.connection_id,
        now=p.clock.now(),
        owner="rt15",
    )
    return t


async def test_rt15_batch_a_tables_are_isolated_per_user(isolated_db: TempDatabase) -> None:
    async with pipeline(isolated_db) as p:
        a = await _tenant_with_mail(p, "avery@brightwater.example", "Avery Lindqvist", 6)
        b = await _tenant_with_mail(p, "blake@tallgrass.example", "Blake Ortiz", 4)
        await p.drain()
        for table in TABLES:
            total_a = p.scalar(f"SELECT count(*) FROM {table} WHERE user_id = %s", (a.user_id,))
            total_b = p.scalar(f"SELECT count(*) FROM {table} WHERE user_id = %s", (b.user_id,))
            assert total_a > 0 and total_b > 0, table
            for factory in (p.api, p.worker):
                async with factory(user_id=None) as uow:
                    assert (
                        await uow.session.execute(text(f"SELECT count(*) FROM {table}"))
                    ).scalar_one() == 0
                async with factory(user_id=a.user_id) as uow:
                    rows = (await uow.session.execute(text(f"SELECT DISTINCT user_id FROM {table}"))).all()
                    assert [r[0] for r in rows] == [a.user_id], table
                    count = (await uow.session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
                    assert count == total_a, table


async def test_rt15_writes_for_another_user_are_rejected(isolated_db: TempDatabase) -> None:
    async with pipeline(isolated_db) as p:
        a = await p.tenant(email="avery@brightwater.example", name="Avery")
        b = await p.tenant(email="blake@tallgrass.example", name="Blake")
        statements = {
            "organizations": "INSERT INTO organizations (id, user_id, name) "
            "VALUES (gen_random_uuid(), :u, 'x')",
            "persons": "INSERT INTO persons (id, user_id) VALUES (gen_random_uuid(), :u)",
            "source_items": "INSERT INTO source_items (id, user_id, kind, provider, external_id, "
            "content_hash, occurred_at) "
            "VALUES (gen_random_uuid(), :u, 'message', 'fake', 'x', '\\x00', now())",
            "users": "UPDATE users SET display_name = 'hijacked' WHERE id = :u",
        }
        for factory in (p.api, p.worker):
            for table, sql in statements.items():
                async with factory(user_id=a.user_id) as uow:
                    if table == "users":
                        if factory is p.worker:
                            with pytest.raises(ProgrammingError, match="permission denied"):
                                await uow.session.execute(text(sql), {"u": b.user_id})
                        else:
                            result = await uow.session.execute(text(sql), {"u": b.user_id})
                            assert result.rowcount == 0  # type: ignore[attr-defined]  # B's row is invisible
                        continue
                    with pytest.raises(DBAPIError, match="row-level security"):
                        await uow.session.execute(text(sql), {"u": b.user_id})
        assert p.scalar("SELECT count(*) FROM users WHERE display_name = 'hijacked'") == 0


async def test_rt15_worker_enumerates_users_without_reading_content(isolated_db: TempDatabase) -> None:
    async with pipeline(isolated_db) as p:
        a = await p.tenant(email="avery@brightwater.example", name="Avery")
        b = await p.tenant(email="blake@tallgrass.example", name="Blake")
        async with p.worker(user_id=None) as uow:
            ids = {r[0] for r in (await uow.session.execute(text("SELECT id FROM users"))).all()}
            assert {a.user_id, b.user_id} <= ids
            tz = await uow.session.execute(text("SELECT timezone FROM users WHERE id = :u"), {"u": b.user_id})
            assert tz.scalar_one() == "America/Los_Angeles"
        for column in ("email", "display_name", "*"):
            async with p.worker(user_id=a.user_id) as uow:
                with pytest.raises(ProgrammingError, match="permission denied"):
                    await uow.session.execute(text(f"SELECT {column} FROM users"))
        async with p.api(user_id=None) as uow:
            assert (await uow.session.execute(text("SELECT count(*) FROM users"))).scalar_one() == 0
        async with p.api(user_id=a.user_id) as uow:
            emails = (await uow.session.execute(text("SELECT email FROM users"))).all()
            assert [r[0] for r in emails] == ["avery@brightwater.example"]
