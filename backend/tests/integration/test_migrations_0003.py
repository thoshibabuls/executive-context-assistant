"""Slice 0.3 migration chain: ordering, objects, constraints, role checks (BACKEND_DESIGN.md §7.6, §7.7)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from psycopg import sql

from eca.platform.roles import RuntimeRoleError
from tests.conftest import RUNTIME_ROLE, WORKER_ROLE, TempDatabase, alembic_config

pytestmark = pytest.mark.db


def _revision(admin_url: str) -> str | None:
    with psycopg.connect(admin_url) as conn:
        if conn.execute("SELECT to_regclass('public.alembic_version')").fetchone() == (None,):
            return None
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        return row[0] if row else None


def _tables(admin_url: str) -> set[str]:
    with psycopg.connect(admin_url) as conn:
        rows = conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'").fetchall()
    return {r[0] for r in rows}


def test_chain_is_linear_and_ordered() -> None:
    script = ScriptDirectory.from_config(alembic_config("postgresql://unused/unused"))
    chain = [rev.revision for rev in reversed(list(script.walk_revisions()))]
    assert chain == ["0001", "0002", "0003", "0004", "0005"]
    assert script.get_heads() == ["0005"]


def test_each_revision_up_down_up(fresh_db: TempDatabase) -> None:
    cfg = alembic_config(fresh_db.admin_url)
    expected = {
        "0001": {"alembic_version"},
        "0002": {"alembic_version"},
        "0003": {"alembic_version", "outbox", "event_consumptions"},
        "0004": {
            "alembic_version",
            "outbox",
            "event_consumptions",
            "procrastinate_jobs",
            "procrastinate_events",
            "procrastinate_periodic_defers",
            "procrastinate_workers",
        },
    }
    expected["0005"] = expected["0004"] | {"ai_calls", "ai_cost_rollups"}
    for rev, tables in expected.items():
        command.upgrade(cfg, rev)
        assert _revision(fresh_db.admin_url) == rev
        assert _tables(fresh_db.admin_url) == tables
    for rev in ("0004", "0003", "0002", "0001"):
        command.downgrade(cfg, rev)
        assert _revision(fresh_db.admin_url) == rev
        assert _tables(fresh_db.admin_url) == expected[rev]
    command.downgrade(cfg, "base")
    with psycopg.connect(fresh_db.admin_url) as conn:
        leftovers = conn.execute(
            "SELECT count(*) FROM pg_proc WHERE proname LIKE 'procrastinate%%' "
            "UNION ALL SELECT count(*) FROM pg_type WHERE typname LIKE 'procrastinate%%' "
            "UNION ALL SELECT count(*) FROM pg_default_acl"
        ).fetchall()
    assert leftovers == [(0,), (0,), (0,)]
    command.upgrade(cfg, "head")
    assert _revision(fresh_db.admin_url) == "0005"


def test_outbox_schema_matches_design(migrated_db: TempDatabase) -> None:
    with psycopg.connect(migrated_db.admin_url) as conn:
        columns = conn.execute(
            """
            SELECT column_name, data_type, is_nullable, column_default
              FROM information_schema.columns WHERE table_name = 'outbox' ORDER BY ordinal_position
            """
        ).fetchall()
        indexes = dict(
            conn.execute("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'outbox'").fetchall()
        )
        constraints = dict(
            conn.execute(
                "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid IN ('outbox'::regclass, 'event_consumptions'::regclass)"
            ).fetchall()
        )
    assert [(c[0], c[1], c[2]) for c in columns] == [
        ("id", "uuid", "NO"),
        ("user_id", "uuid", "YES"),
        ("event_type", "text", "NO"),
        ("aggregate_type", "text", "NO"),
        ("aggregate_id", "uuid", "NO"),
        ("payload", "jsonb", "NO"),
        ("correlation", "jsonb", "NO"),
        ("status", "text", "NO"),
        ("attempts", "integer", "NO"),
        ("next_attempt_at", "timestamp with time zone", "NO"),
        ("last_error", "text", "YES"),
        ("created_at", "timestamp with time zone", "NO"),
        ("dispatched_at", "timestamp with time zone", "YES"),
    ]
    defaults = {c[0]: c[3] for c in columns}
    assert defaults["status"] == "'pending'::text"
    assert defaults["attempts"] == "0"
    assert defaults["next_attempt_at"] == defaults["created_at"] == "now()"
    assert "WHERE (status = 'pending'::text)" in indexes["ix_outbox_pending"]
    assert "(next_attempt_at)" in indexes["ix_outbox_pending"]
    assert "WHERE (user_id IS NOT NULL)" in indexes["ix_outbox_user"]
    assert constraints["ck_outbox_status"] == (
        "CHECK ((status = ANY (ARRAY['pending'::text, 'dispatched'::text, 'failed'::text])))"
    )
    assert constraints["pk_event_consumptions"] == "PRIMARY KEY (event_id, handler)"
    assert constraints["fk_event_consumptions_event"] == "FOREIGN KEY (event_id) REFERENCES outbox(id)"
    # Deferred FK (BACKEND_DESIGN.md §7.3.1): no foreign key from outbox to anything yet.
    assert not [name for name, d in constraints.items() if name.startswith("fk_outbox")]
    assert "users" not in _tables(migrated_db.admin_url)


def test_event_consumptions_reject_unknown_event(migrated_db: TempDatabase) -> None:
    with pytest.raises(psycopg.errors.ForeignKeyViolation), psycopg.connect(migrated_db.admin_url) as conn:
        conn.execute("INSERT INTO event_consumptions (event_id, handler) VALUES (%s, 'h')", (uuid.uuid4(),))


@pytest.fixture
def scratch_roles(admin_base_url: str, fresh_db: TempDatabase) -> Iterator[list[str]]:
    """Cluster roles created by a test; dropped afterwards (with anything granted to them)."""
    created: list[str] = []
    yield created
    with psycopg.connect(fresh_db.admin_url, autocommit=True) as conn:
        for role in created:
            conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
    with psycopg.connect(admin_base_url, autocommit=True) as conn:
        for role in created:
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def _create_role(fresh_db: TempDatabase, scratch: list[str], attrs: str) -> str:
    role = f"eca_t_{uuid.uuid4().hex[:10]}"
    scratch.append(role)
    with psycopg.connect(fresh_db.admin_url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE ROLE {} " + attrs).format(sql.Identifier(role)))
    return role


def _upgrade_fails(cfg: Config, fresh_db: TempDatabase, message: str) -> None:
    with pytest.raises(RuntimeRoleError, match=message):
        command.upgrade(cfg, "head")
    assert _revision(fresh_db.admin_url) == "0001"  # 0002 rolled back as a whole
    assert "outbox" not in _tables(fresh_db.admin_url)


def test_missing_worker_role_fails_loudly(fresh_db: TempDatabase) -> None:
    cfg = alembic_config(fresh_db.admin_url, worker_role="eca_no_such_worker")
    _upgrade_fails(cfg, fresh_db, "'eca_no_such_worker' does not exist")


def test_same_role_for_api_and_worker_fails(fresh_db: TempDatabase) -> None:
    cfg = alembic_config(fresh_db.admin_url, worker_role=RUNTIME_ROLE)
    _upgrade_fails(cfg, fresh_db, "must differ")


@pytest.mark.parametrize("attrs, flag", [("BYPASSRLS", "BYPASSRLS"), ("CREATEROLE", "CREATEROLE")])
def test_unsafe_worker_role_fails(
    fresh_db: TempDatabase, scratch_roles: list[str], attrs: str, flag: str
) -> None:
    role = _create_role(fresh_db, scratch_roles, f"LOGIN NOSUPERUSER {attrs}")
    cfg = alembic_config(fresh_db.admin_url, worker_role=role)
    _upgrade_fails(cfg, fresh_db, f"must not have {flag}")


def test_nested_runtime_roles_fail(fresh_db: TempDatabase, scratch_roles: list[str]) -> None:
    worker = _create_role(fresh_db, scratch_roles, "LOGIN NOSUPERUSER NOBYPASSRLS")
    api = _create_role(fresh_db, scratch_roles, "LOGIN NOSUPERUSER NOBYPASSRLS")
    with psycopg.connect(fresh_db.admin_url, autocommit=True) as conn:
        conn.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(worker), sql.Identifier(api)))
    command.upgrade(alembic_config(fresh_db.admin_url, runtime_role=api, worker_role=worker), "0001")
    _upgrade_fails(
        alembic_config(fresh_db.admin_url, runtime_role=api, worker_role=worker), fresh_db, "member of"
    )


def test_migrations_do_not_create_roles(fresh_db: TempDatabase) -> None:
    with psycopg.connect(fresh_db.admin_url) as conn:
        before = {r[0] for r in conn.execute("SELECT rolname FROM pg_roles").fetchall()}
    command.upgrade(alembic_config(fresh_db.admin_url), "head")
    with psycopg.connect(fresh_db.admin_url) as conn:
        after = {r[0] for r in conn.execute("SELECT rolname FROM pg_roles").fetchall()}
    assert after == before
    assert {RUNTIME_ROLE, WORKER_ROLE} <= before
