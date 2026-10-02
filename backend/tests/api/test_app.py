"""Health endpoints, request IDs and Problem Details translation (no database)."""

from __future__ import annotations

import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from eca.api.app import create_app
from eca.platform import errors
from eca.platform.config import Settings

HEAD = "0012"


def _client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def test_healthz(settings_no_db: Settings) -> None:
    with _client(create_app(settings_no_db)) as client:
        r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
    assert r.headers["x-request-id"].startswith("req_")


def test_request_id_is_propagated_when_valid(settings_no_db: Settings) -> None:
    with _client(create_app(settings_no_db)) as client:
        assert (
            client.get("/healthz", headers={"X-Request-Id": "abc12345-req"}).headers["x-request-id"]
            == "abc12345-req"
        )
        assert (
            client.get("/healthz", headers={"X-Request-Id": "bad id!"})
            .headers["x-request-id"]
            .startswith("req_")
        )


def test_readyz_without_database_is_503(settings_no_db: Settings) -> None:
    with _client(create_app(settings_no_db)) as client:
        r = client.get("/readyz")
    assert r.status_code == 503
    assert r.json()["checks"] == {"database": False, "schema_at_head": False}
    assert r.json()["revision"]["head"] == HEAD


def test_readyz_with_unreachable_database_is_503() -> None:
    settings = Settings(_env_file=None, api_database_url="postgresql://nobody:x@127.0.0.1:1/none")  # type: ignore[call-arg]
    with _client(create_app(settings)) as client:
        started = time.monotonic()
        r = client.get("/readyz")
        elapsed = time.monotonic() - started
    assert r.status_code == 503
    assert r.json()["checks"]["database"] is False
    assert elapsed < 10, f"readiness probe must be bounded, took {elapsed:.1f}s"


class _Body(BaseModel):
    name: str
    count: int


def _app_with_error_routes(settings: Settings) -> FastAPI:
    app = create_app(settings)

    @app.get("/raise/not-found")
    async def nf() -> None:
        raise errors.NotFound("no such item")

    @app.get("/raise/conflict")
    async def conflict() -> None:
        raise errors.Conflict("due_at changed since version 6", fields=["due_at"], current_version=8)

    @app.get("/raise/precondition")
    async def pre() -> None:
        raise errors.PreconditionFailed("stale", reason="version")

    @app.get("/raise/scope")
    async def scope() -> None:
        raise errors.PreconditionFailed("gmail not granted", reason="scope_required")

    @app.get("/raise/rate")
    async def rate() -> None:
        raise errors.BudgetExceeded("daily cap", retry_after_s=60)

    @app.get("/raise/reauth")
    async def reauth() -> None:
        raise errors.AuthRevoked("token revoked")

    @app.get("/raise/gone")
    async def gone() -> None:
        raise errors.Gone("deleting")

    @app.get("/raise/cursor")
    async def cursor() -> None:
        raise errors.CursorExpired("internal")

    @app.get("/raise/boom")
    async def boom() -> None:
        raise RuntimeError("secret internal detail")

    @app.post("/validate")
    async def validate(body: _Body) -> dict[str, str]:
        return {"ok": body.name}

    return app


def test_domain_errors_map_to_problem_details(settings_no_db: Settings) -> None:
    expected = {
        "/raise/not-found": (404, "not_found"),
        "/raise/conflict": (409, "conflict"),
        "/raise/precondition": (412, "precondition_failed"),
        "/raise/scope": (409, "scope_required"),
        "/raise/rate": (429, "budget_exceeded"),
        "/raise/reauth": (409, "reauth_required"),
        "/raise/gone": (410, "gone"),
        "/raise/cursor": (500, "internal_error"),
    }
    with _client(_app_with_error_routes(settings_no_db)) as client:
        for path, (status, code) in expected.items():
            r = client.get(path)
            assert r.status_code == status, path
            assert r.headers["content-type"].startswith("application/problem+json"), path
            body = r.json()
            assert body["code"] == code and body["status"] == status and body["instance"] == path
            assert body["request_id"] == r.headers["x-request-id"]
        conflict = client.get("/raise/conflict").json()
        assert conflict["current_version"] == 8
        assert conflict["errors"] == [{"field": "due_at", "message": "changed by another update"}]
        assert client.get("/raise/rate").headers["retry-after"] == "60"


def test_unhandled_errors_do_not_leak_details(settings_no_db: Settings) -> None:
    with _client(_app_with_error_routes(settings_no_db)) as client:
        r = client.get("/raise/boom")
    assert r.status_code == 500
    assert "secret internal detail" not in r.text
    assert r.json()["code"] == "internal_error"


def test_validation_errors_are_field_level(settings_no_db: Settings) -> None:
    with _client(_app_with_error_routes(settings_no_db)) as client:
        r = client.post("/validate", json={"name": "x", "count": "not-a-number"})
    assert r.status_code == 422
    assert r.headers["content-type"].startswith("application/problem+json")
    assert [e["field"] for e in r.json()["errors"]] == ["count"]


def test_unknown_route_is_problem_json(settings_no_db: Settings) -> None:
    with _client(create_app(settings_no_db)) as client:
        r = client.get("/nope")
    assert r.status_code == 404
    assert r.json()["code"] == "not_found"
