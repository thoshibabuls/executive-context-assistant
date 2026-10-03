"""Alembic baseline: upgrade, downgrade, re-upgrade; extensions and RLS helper present."""

from __future__ import annotations

import psycopg
import pytest
from alembic import command
from fastapi.testclient import TestClient

from eca.api.app import create_app
from eca.platform.config import Settings
from tests.conftest import RUNTIME_ROLE, TempDatabase, alembic_config

pytestmark = pytest.mark.db

HEAD = "0026"


def _state(admin_url: str) -> dict[str, object]:
    with psycopg.connect(admin_url) as conn:
        exts = dict(conn.execute("SELECT extname, extversion FROM pg_extension").fetchall())
        fn = conn.execute("SELECT to_regprocedure('eca_current_user_id()')").fetchone()
        version = conn.execute("SELECT to_regclass('public.alembic_version')").fetchone()
        rev = None
        if version and version[0]:
            row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
            rev = row[0] if row else None
        can_exec = None
        if fn and fn[0]:
            can_exec = conn.execute(
                "SELECT has_function_privilege(%s, 'eca_current_user_id()', 'EXECUTE')", (RUNTIME_ROLE,)
            ).fetchone()
    return {
        "extensions": exts,
        "function": bool(fn and fn[0]),
        "revision": rev,
        "runtime_can_execute": bool(can_exec and can_exec[0]),
    }


def test_upgrade_downgrade_upgrade(fresh_db: TempDatabase) -> None:
    cfg = alembic_config(fresh_db.admin_url)

    command.upgrade(cfg, "head")
    state = _state(fresh_db.admin_url)
    exts = state["extensions"]
    assert isinstance(exts, dict)
    assert {"vector", "pg_trgm", "citext"} <= set(exts)
    major, minor = (int(p) for p in str(exts["vector"]).split(".")[:2])
    assert (major, minor) >= (0, 8)
    assert state["function"] is True
    assert state["runtime_can_execute"] is True
    assert state["revision"] == HEAD

    command.downgrade(cfg, "base")
    state = _state(fresh_db.admin_url)
    assert state["function"] is False
    assert state["revision"] is None
    assert "vector" not in state["extensions"]  # type: ignore[operator]

    command.upgrade(cfg, "head")
    assert _state(fresh_db.admin_url)["revision"] == HEAD


def test_current_user_id_is_null_when_unset(migrated_db: TempDatabase) -> None:
    with psycopg.connect(migrated_db.runtime_url) as conn:
        assert conn.execute("SELECT eca_current_user_id()").fetchone() == (None,)


def test_readyz_reports_ready_on_migrated_database(migrated_db: TempDatabase) -> None:
    settings = Settings(_env_file=None, api_database_url=migrated_db.runtime_url)  # type: ignore[call-arg]
    with TestClient(create_app(settings)) as client:
        r = client.get("/readyz")
    assert r.status_code == 200, r.text
    assert r.json() == {
        "status": "ready",
        "checks": {"database": True, "schema_at_head": True},
        "revision": {"current": HEAD, "head": HEAD},
    }


def test_readyz_not_ready_before_migrations(fresh_db: TempDatabase) -> None:
    settings = Settings(_env_file=None, api_database_url=fresh_db.runtime_url)  # type: ignore[call-arg]
    with TestClient(create_app(settings)) as client:
        r = client.get("/readyz")
    assert r.status_code == 503
    assert r.json()["checks"] == {"database": True, "schema_at_head": False}
