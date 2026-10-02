from __future__ import annotations

import pytest

from eca.platform import errors
from eca.platform.config import Settings
from eca.platform.db import to_psycopg_url
from eca.platform.rls import runtime_role_grants_ddl, user_isolation_ddl


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://u:p@h:5432/db",
        "postgres://u:p@h/db",
        "postgresql+asyncpg://u:p@h/db",
        "postgresql+psycopg://u:p@h/db",
    ],
)
def test_database_urls_are_normalized_to_psycopg(url: str) -> None:
    assert to_psycopg_url(url).startswith("postgresql+psycopg://")


@pytest.mark.parametrize("url", ["sqlite:///x.db", "mysql://u@h/db", "not a url"])
def test_non_postgres_urls_are_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        to_psycopg_url(url)


def test_secrets_are_not_rendered() -> None:
    s = Settings(
        _env_file=None, gemini_api_key="AIza-not-a-real-key", api_database_url="postgresql://u:pw@h/db"
    )  # type: ignore[call-arg]
    rendered = repr(s) + str(s.model_dump())
    assert "AIza-not-a-real-key" not in rendered
    assert "pw@h" not in rendered


def test_cors_origins_parsing() -> None:
    s = Settings(_env_file=None, api_cors_origins=" http://a , ,http://b")  # type: ignore[call-arg]
    assert s.cors_origins == ["http://a", "http://b"]


def test_user_isolation_ddl_is_fail_closed_and_forced() -> None:
    stmts = user_isolation_ddl("work_items")
    assert stmts[0] == "ALTER TABLE work_items ENABLE ROW LEVEL SECURITY"
    assert stmts[1] == "ALTER TABLE work_items FORCE ROW LEVEL SECURITY"
    assert "USING (user_id = eca_current_user_id())" in stmts[2]
    assert "WITH CHECK (user_id = eca_current_user_id())" in stmts[2]


@pytest.mark.parametrize("bad", ["x; DROP TABLE users", "Users", "a-b", "", "1abc"])
def test_rls_helpers_reject_unsafe_identifiers(bad: str) -> None:
    with pytest.raises(ValueError):
        user_isolation_ddl(bad)
    with pytest.raises(ValueError):
        runtime_role_grants_ddl(bad)


def test_runtime_grants_are_dml_only() -> None:
    joined = " ".join(runtime_role_grants_ddl("eca_app")).upper()
    for forbidden in ("CREATE ON", "TRUNCATE", "REFERENCES", "TRIGGER", "ALL PRIVILEGES", "BYPASSRLS"):
        assert forbidden not in joined


def test_domain_errors_carry_no_http_concepts() -> None:
    err = errors.Conflict("changed", fields=["due_at"], current_version=8)
    assert not hasattr(err, "status_code")
    assert err.fields == ["due_at"]
    assert issubclass(errors.BudgetExceeded, errors.RateLimited)


def test_uvicorn_loop_factory_is_selector_based() -> None:
    """uvicorn on Windows defaults to ProactorEventLoop, which psycopg 3 async cannot use."""
    import asyncio

    import uvicorn

    from eca.platform.runtime import selector_loop_factory

    factory = uvicorn.Config(
        "eca.api.app:app", loop="eca.platform.runtime:selector_loop_factory"
    ).get_loop_factory()
    assert factory is selector_loop_factory
    loop = factory()
    try:
        assert isinstance(loop, asyncio.SelectorEventLoop)
    finally:
        loop.close()
