"""Shared fixtures.

Database tests need ``ECA_TEST_DATABASE_URL``: a PostgreSQL (with pgvector >= 0.8) URL for a role
that can create databases and roles (docker compose ``postgres`` user, or the CI service).
Each session creates a throwaway database, migrates it as the owner, and connects the
application as a separate non-superuser runtime role so RLS is genuinely enforced.

Outside CI, database tests are skipped when the variable is absent; in CI (``CI=true``) a
missing database is an error, never a silent skip.
"""

from __future__ import annotations

import os
import uuid
from argparse import Namespace
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy.engine import make_url

from eca.platform.config import Settings
from eca.platform.runtime import ensure_selector_event_loop_policy

ensure_selector_event_loop_policy()

BACKEND_DIR = Path(__file__).resolve().parents[1]
RUNTIME_ROLE = "eca_test_app"
RUNTIME_PASSWORD = "eca_test_app_pw"  # test-only role on a throwaway database


@dataclass(frozen=True)
class TempDatabase:
    admin_url: str  # owner/superuser, plain postgresql:// URL
    runtime_url: str  # non-superuser runtime role
    name: str


def _admin_base_url() -> str:
    url = os.environ.get("ECA_TEST_DATABASE_URL")
    if not url:
        if os.environ.get("CI"):
            pytest.fail("ECA_TEST_DATABASE_URL is required in CI")
        pytest.skip("ECA_TEST_DATABASE_URL not set")
    return url


def _plain(url: str) -> str:
    return make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)


def _with_db(url: str, database: str, *, user: str | None = None, password: str | None = None) -> str:
    u = make_url(url).set(drivername="postgresql", database=database)
    if user is not None:
        u = u.set(username=user, password=password)
    return u.render_as_string(hide_password=False)


def alembic_config(admin_url: str, runtime_role: str = RUNTIME_ROLE) -> Config:
    cfg = Config(
        str(BACKEND_DIR / "alembic.ini"),
        cmd_opts=Namespace(x=[f"db_url={admin_url}", f"runtime_role={runtime_role}"]),
    )
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    return cfg


def _create_database(base_admin: str) -> tuple[str, str]:
    name = f"eca_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(_plain(base_admin), autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (RUNTIME_ROLE,)).fetchone()
        if not exists:
            conn.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE"
                ).format(sql.Identifier(RUNTIME_ROLE), sql.Literal(RUNTIME_PASSWORD))
            )
        conn.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(name), sql.Identifier(RUNTIME_ROLE)
            )
        )
    return name, _with_db(base_admin, name)


def _drop_database(base_admin: str, name: str) -> None:
    with psycopg.connect(_plain(base_admin), autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture(scope="session")
def admin_base_url() -> str:
    return _admin_base_url()


@pytest.fixture(scope="session")
def migrated_db(admin_base_url: str) -> Iterator[TempDatabase]:
    """A database migrated to head, shared by the session (tests must not leave data behind)."""
    name, admin_url = _create_database(admin_base_url)
    try:
        command.upgrade(alembic_config(admin_url), "head")
        yield TempDatabase(
            admin_url=admin_url,
            runtime_url=_with_db(admin_base_url, name, user=RUNTIME_ROLE, password=RUNTIME_PASSWORD),
            name=name,
        )
    finally:
        _drop_database(admin_base_url, name)


@pytest.fixture
def fresh_db(admin_base_url: str) -> Iterator[TempDatabase]:
    """An empty, unmigrated database for migration tests."""
    name, admin_url = _create_database(admin_base_url)
    try:
        yield TempDatabase(
            admin_url=admin_url,
            runtime_url=_with_db(admin_base_url, name, user=RUNTIME_ROLE, password=RUNTIME_PASSWORD),
            name=name,
        )
    finally:
        _drop_database(admin_base_url, name)


@pytest.fixture
def settings_no_db() -> Settings:
    return Settings(_env_file=None, api_env="test", api_database_url=None)  # type: ignore[call-arg]
