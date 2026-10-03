"""Exact privilege matrix at the migration head (slices 0.3 and 0.4, Batch A, Phases 1-4).

BACKEND_DESIGN.md §7.6, "What the test asserts".

Runs on a freshly migrated database with no test tables, after upgrade and again after down/up.
"""

from __future__ import annotations

import psycopg
import pytest
from alembic import command

from tests.conftest import RUNTIME_ROLE, WORKER_ROLE, TempDatabase, alembic_config

pytestmark = pytest.mark.db

TABLE_PRIVS = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
DML = {"SELECT", "INSERT", "UPDATE", "DELETE"}
PROCRASTINATE_TABLES = (
    "procrastinate_events",
    "procrastinate_jobs",
    "procrastinate_periodic_defers",
    "procrastinate_workers",
)

EXPECTED_TABLES: dict[str, dict[str, set[str]]] = {
    "alembic_version": {RUNTIME_ROLE: {"SELECT"}, WORKER_ROLE: {"SELECT"}},
    "outbox": {RUNTIME_ROLE: {"INSERT"}, WORKER_ROLE: set(DML)},
    "event_consumptions": {RUNTIME_ROLE: set(), WORKER_ROLE: {"SELECT", "INSERT", "DELETE"}},
    **{t: {RUNTIME_ROLE: set(), WORKER_ROLE: set(DML)} for t in PROCRASTINATE_TABLES},
    "ai_calls": {RUNTIME_ROLE: {"INSERT"}, WORKER_ROLE: set(DML)},
    "ai_cost_rollups": {RUNTIME_ROLE: {"SELECT"}, WORKER_ROLE: set(DML)},
}
RLS_TABLES = ("outbox", "event_consumptions", "ai_calls", "ai_cost_rollups")
# User-owned business tables: per-user isolation for all roles, DML for both runtime roles (§7.6).
BUSINESS_TABLES = (
    # Batch A
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
    # Phase 1 (0006-0010)
    "auth_sessions",
    "deletion_jobs",
    "idempotency_keys",
    "extractions",
    "work_items",
    "evidence",
    "item_evidence",
    "decisions",
    "context_events",
    "feedback_events",
    "meetings",
    "meeting_participants",
    # Phase 2 (0011-0017)
    "chunks",
    "entity_links",
    "retrieval_traces",
    "user_checkpoints",
    "projects",
    "project_members",
    "chat_sessions",
    "chat_messages",
    # Phase 3 (0018-0023)
    "reminders",
    "notifications",
    "push_subscriptions",
    "briefings",
    "priority_pairs",
    "user_priority_weights",
    # Phase 4 (0024-0027)
    "recordings",
    "transcript_segments",
)
# users: the API keeps DML on its own row; the worker has column SELECT only, plus DELETE of its
# own deleting user's row (slice 1.9) (§7.6 exception).
USERS_WORKER_COLUMNS = {"id", "status", "timezone", "work_hours"}
# Sign-in tables (§7.6): oauth_states exists before a user is known (API only); audit_log is
# API INSERT only, worker SELECT, INSERT, DELETE (retention, account deletion).
EXPECTED_TABLES["oauth_states"] = {RUNTIME_ROLE: set(DML), WORKER_ROLE: set()}
EXPECTED_TABLES["audit_log"] = {RUNTIME_ROLE: {"INSERT"}, WORKER_ROLE: {"SELECT", "INSERT", "DELETE"}}
SIGNIN_RLS_TABLES = ("oauth_states", "audit_log")
INFRASTRUCTURE_TABLES = set(EXPECTED_TABLES)
for _table in BUSINESS_TABLES:
    EXPECTED_TABLES[_table] = {RUNTIME_ROLE: set(DML), WORKER_ROLE: set(DML)}
EXPECTED_TABLES["users"] = {RUNTIME_ROLE: set(DML), WORKER_ROLE: {"DELETE"}}
# SECURITY DEFINER pre-authentication lookups: EXECUTE for the API role only (§7.6).
DEFINER_FUNCTIONS = ("eca_signin_user_id(text,citext)", "eca_session_user_id(bytea)")


def _table_privs(conn: psycopg.Connection, role: str, table: str) -> set[str]:
    return {
        p
        for p in TABLE_PRIVS
        if conn.execute("SELECT has_table_privilege(%s, %s, %s)", (role, f"public.{table}", p)).fetchone()[0]  # type: ignore[index]
    }


def _public_table_privs(conn: psycopg.Connection, table: str) -> set[str]:
    rows = conn.execute(
        "SELECT a.privilege_type FROM pg_class c, aclexplode(c.relacl) a "
        "WHERE c.oid = %s::regclass AND a.grantee = 0",
        (f"public.{table}",),
    ).fetchall()
    return {r[0] for r in rows}


def _assert_matrix(admin_url: str) -> None:
    with psycopg.connect(admin_url) as conn:
        tables = {r[0] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
        # 5. every migrated table is classified: infrastructure, or business with _user_isolation
        assert tables == INFRASTRUCTURE_TABLES | set(BUSINESS_TABLES) | {"users"}
        columns = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT column_name, has_column_privilege(%s, 'public.users', column_name, 'SELECT') "
                "FROM information_schema.columns WHERE table_name = 'users'",
                (WORKER_ROLE,),
            )
        }
        assert {c for c, granted in columns.items() if granted} == USERS_WORKER_COLUMNS

        # 1. tables: API role, worker role and PUBLIC
        for table, expected in EXPECTED_TABLES.items():
            for role in (RUNTIME_ROLE, WORKER_ROLE):
                assert _table_privs(conn, role, table) == expected[role], (table, role)
            assert _public_table_privs(conn, table) == set(), table

        # sequences
        sequences = [
            r[0] for r in conn.execute("SELECT sequencename FROM pg_sequences WHERE schemaname = 'public'")
        ]
        assert sequences and all(s.startswith("procrastinate_") for s in sequences)
        for seq in sequences:
            for priv in ("USAGE", "SELECT", "UPDATE"):
                api, worker = (
                    conn.execute(
                        "SELECT has_sequence_privilege(%s, %s, %s)", (r, f"public.{seq}", priv)
                    ).fetchone()[0]  # type: ignore[index]
                    for r in (RUNTIME_ROLE, WORKER_ROLE)
                )
                assert api is False, (seq, priv)
                assert worker is (priv != "UPDATE"), (seq, priv)

        # functions: procrastinate_* worker only; eca_current_user_id() both runtime roles; never PUBLIC
        functions = [
            r[0]
            for r in conn.execute(
                "SELECT p.oid::regprocedure::text FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' "
                "AND (p.proname LIKE 'procrastinate%%' OR p.proname = 'eca_current_user_id')"
            )
        ]
        assert len(functions) > 10
        for fn in functions:
            api, worker = (
                conn.execute("SELECT has_function_privilege(%s, %s, 'EXECUTE')", (r, fn)).fetchone()[0]  # type: ignore[index]
                for r in (RUNTIME_ROLE, WORKER_ROLE)
            )
            public = conn.execute(
                "SELECT count(*) FROM pg_proc p, "
                "aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a "
                "WHERE p.oid = %s::regprocedure AND a.grantee = 0",
                (fn,),
            ).fetchone()[0]  # type: ignore[index]
            assert public == 0, fn
            assert worker is True, fn
            assert api is (fn == "eca_current_user_id()"), fn

        for fn in DEFINER_FUNCTIONS:
            definer, public = conn.execute(
                "SELECT p.prosecdef, (SELECT count(*) FROM aclexplode(coalesce(p.proacl, "
                "acldefault('f', p.proowner))) a WHERE a.grantee = 0) FROM pg_proc p "
                "WHERE p.oid = %s::regprocedure",
                (fn,),
            ).fetchone()  # type: ignore[misc]
            assert (definer, public) == (True, 0), fn
            api, worker = (
                conn.execute("SELECT has_function_privilege(%s, %s, 'EXECUTE')", (r, fn)).fetchone()[0]  # type: ignore[index]
                for r in (RUNTIME_ROLE, WORKER_ROLE)
            )
            assert (api, worker) == (True, False), fn

        # schema: USAGE, never CREATE
        for role in (RUNTIME_ROLE, WORKER_ROLE):
            usage, create = conn.execute(
                "SELECT has_schema_privilege(%s, 'public', 'USAGE'), "
                "has_schema_privilege(%s, 'public', 'CREATE')",
                (role, role),
            ).fetchone()  # type: ignore[misc]
            assert (usage, create) == (True, False), role

        # 2. default privileges of the migration role
        defaults = {
            (r[0], r[1], r[2])
            for r in conn.execute(
                "SELECT d.defaclobjtype::text, a.grantee::regrole::text, a.privilege_type "
                "FROM pg_default_acl d, aclexplode(d.defaclacl) a WHERE d.defaclrole = current_user::regrole"
            )
        }
        expected_defaults = {("r", role, p) for role in (RUNTIME_ROLE, WORKER_ROLE) for p in DML} | {
            ("S", role, p) for role in (RUNTIME_ROLE, WORKER_ROLE) for p in ("USAGE", "SELECT")
        }
        assert defaults == expected_defaults

        # 3. RLS and policies
        rls = {
            r[0]: (r[1], r[2])
            for r in conn.execute(
                "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = ANY(%s)",
                (list(RLS_TABLES),),
            )
        }
        assert rls == {t: (True, True) for t in RLS_TABLES}
        signin_rls = {
            r[0]: (r[1], r[2])
            for r in conn.execute(
                "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = ANY(%s)",
                (list(SIGNIN_RLS_TABLES),),
            )
        }
        assert signin_rls == {t: (True, True) for t in SIGNIN_RLS_TABLES}
        business_rls = {
            r[0]: (r[1], r[2])
            for r in conn.execute(
                "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = ANY(%s)",
                ([*BUSINESS_TABLES, "users"],),
            )
        }
        assert business_rls == {t: (True, True) for t in [*BUSINESS_TABLES, "users"]}
        policies = {
            (r[0], r[1], r[2], tuple(r[3]), r[4], r[5])
            for r in conn.execute(
                "SELECT tablename, policyname, cmd, roles, qual, with_check FROM pg_policies "
                "WHERE schemaname = 'public'"
            )
        }
        assert policies == {
            (
                "outbox",
                "outbox_api_insert",
                "INSERT",
                (RUNTIME_ROLE,),
                None,
                "(user_id = eca_current_user_id())",
            ),
            ("outbox", "outbox_worker_all", "ALL", (WORKER_ROLE,), "true", "true"),
            ("event_consumptions", "event_consumptions_worker_all", "ALL", (WORKER_ROLE,), "true", "true"),
            (
                "ai_calls",
                "ai_calls_api_insert",
                "INSERT",
                (RUNTIME_ROLE,),
                None,
                "(user_id = eca_current_user_id())",
            ),
            ("ai_calls", "ai_calls_worker_all", "ALL", (WORKER_ROLE,), "true", "true"),
            (
                "ai_cost_rollups",
                "ai_cost_rollups_api_read",
                "SELECT",
                (RUNTIME_ROLE,),
                "(user_id = eca_current_user_id())",
                None,
            ),
            ("ai_cost_rollups", "ai_cost_rollups_worker_all", "ALL", (WORKER_ROLE,), "true", "true"),
            *(
                (
                    t,
                    f"{t}_user_isolation",
                    "ALL",
                    ("public",),
                    "(user_id = eca_current_user_id())",
                    "(user_id = eca_current_user_id())",
                )
                for t in BUSINESS_TABLES
            ),
            (
                "users",
                "users_user_isolation",
                "ALL",
                ("public",),
                "(id = eca_current_user_id())",
                "(id = eca_current_user_id())",
            ),
            ("users", "users_worker_enumerate", "SELECT", (WORKER_ROLE,), "true", None),
            (
                "users",
                "users_worker_delete",
                "DELETE",
                (WORKER_ROLE,),
                "((id = eca_current_user_id()) AND (status = 'deleting'::text))",
                None,
            ),
            ("oauth_states", "oauth_states_api_all", "ALL", (RUNTIME_ROLE,), "true", "true"),
            (
                "audit_log",
                "audit_log_api_insert",
                "INSERT",
                (RUNTIME_ROLE,),
                None,
                "((user_id IS NULL) OR (user_id = eca_current_user_id()))",
            ),
            ("audit_log", "audit_log_worker_all", "ALL", (WORKER_ROLE,), "true", "true"),
        }

        # 4. role attributes and separation
        for role in (RUNTIME_ROLE, WORKER_ROLE):
            attrs = conn.execute(
                "SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname = %s",
                (role,),
            ).fetchone()
            assert attrs == (False, False, False, False), role
        nested = conn.execute(
            "SELECT pg_has_role(%s, %s, 'MEMBER'), pg_has_role(%s, %s, 'MEMBER')",
            (RUNTIME_ROLE, WORKER_ROLE, WORKER_ROLE, RUNTIME_ROLE),
        ).fetchone()
        assert nested == (False, False)


def test_privilege_matrix_after_upgrade_and_after_down_up(fresh_db: TempDatabase) -> None:
    cfg = alembic_config(fresh_db.admin_url)
    command.upgrade(cfg, "head")
    _assert_matrix(fresh_db.admin_url)
    command.downgrade(cfg, "0001")
    command.upgrade(cfg, "head")
    _assert_matrix(fresh_db.admin_url)


def test_alembic_version_drift_from_0001_is_corrected(fresh_db: TempDatabase) -> None:
    """0001 left the API role with DML on alembic_version; 0002 reduces it to SELECT."""
    cfg = alembic_config(fresh_db.admin_url)
    command.upgrade(cfg, "0001")
    with psycopg.connect(fresh_db.admin_url) as conn:
        assert _table_privs(conn, RUNTIME_ROLE, "alembic_version") == DML  # the drift being fixed
    command.upgrade(cfg, "0002")
    with psycopg.connect(fresh_db.admin_url) as conn:
        assert _table_privs(conn, RUNTIME_ROLE, "alembic_version") == {"SELECT"}
        assert _table_privs(conn, WORKER_ROLE, "alembic_version") == {"SELECT"}
