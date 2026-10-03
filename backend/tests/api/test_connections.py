"""Slice 1.2 connections with a mocked Google (IMPLEMENTATION_PLAN.md §3, TECHNICAL_DESIGN.md §17).

Connect with incremental scopes, envelope-encrypted refresh tokens that are never returned or
logged, capability gating on granted scopes (denied scopes), disconnect with revocation, and
``needs_reauth`` on ``invalid_grant`` (persisted even though the token request fails). All tokens
and identities are synthetic.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import UUID

import pytest

from eca.connectors import CALENDAR_READONLY, GMAIL_READONLY, ConnectionInfo
from eca.ingestion import MAIL_RESOURCE, build_connector_registry
from eca.platform.db import create_engine, create_session_factory
from eca.platform.errors import AuthRevoked
from eca.platform.uow import UnitOfWorkFactory
from tests.api.google_fake import EMAIL, GoogleHarness, google_harness
from tests.conftest import TempDatabase

pytestmark = pytest.mark.db

REFRESH = "rt-test-refresh-0001"


@pytest.fixture
def g(isolated_db: TempDatabase, tmp_path: Path) -> Iterator[GoogleHarness]:
    with google_harness(isolated_db, tmp_path) as harness:
        yield harness


def _connect(g: GoogleHarness, capability: str, granted: str) -> Any:
    r = g.client.post(
        "/api/v1/connections", json={"provider": "google", "capability": capability}, headers=g.csrf()
    )
    assert r.status_code == 200, r.text
    url = urlparse(r.json()["authorization_url"])
    query = {k: v[0] for k, v in parse_qs(url.query).items()}
    g.google.nonce = query["nonce"]
    g.google.granted_scope = granted
    cb = g.client.get(
        "/api/v1/connections/google/callback",
        params={"code": f"code-{capability}", "state": query["state"]},
        follow_redirects=False,
    )
    return query, cb


def test_connect_mail_then_calendar_with_incremental_scopes(
    g: GoogleHarness, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    assert g.sign_in().status_code == 302
    query, cb = _connect(g, "mail", f"openid email {GMAIL_READONLY}")
    assert GMAIL_READONLY in query["scope"] and CALENDAR_READONLY not in query["scope"]
    assert query["access_type"] == "offline"
    assert cb.status_code == 302 and cb.headers["location"].endswith("/settings/connections")
    listed = g.client.get("/api/v1/connections").json()["items"]
    assert [(c["account_email"], c["status"], c["capabilities"]) for c in listed] == [
        (EMAIL, "active", ["mail"])
    ]
    assert REFRESH not in str(listed)
    row = g.sql("SELECT refresh_token_ciphertext, token_key_version, granted_scopes FROM connections")[0]
    assert REFRESH.encode() not in bytes(row[0]) and row[1] == 1
    # A sync is requested for the granted capability only.
    assert g.sql("SELECT payload->>'resource' FROM outbox WHERE event_type = 'SyncRequested'") == [
        (MAIL_RESOURCE,)
    ]

    # Calendar later: no new refresh token is returned; the stored one is kept and scopes are merged.
    g.google.refresh_token = None
    _, cb = _connect(g, "calendar", f"openid email {CALENDAR_READONLY}")
    assert cb.status_code == 302
    listed = g.client.get("/api/v1/connections").json()["items"]
    assert len(listed) == 1 and sorted(listed[0]["capabilities"]) == ["calendar", "mail"]
    assert bytes(g.sql("SELECT refresh_token_ciphertext FROM connections")[0][0]) == bytes(row[0])
    assert sorted(g.client.get("/api/v1/me").json()["capabilities"]) == ["calendar", "mail"]
    # Tokens never reach the logs or audit rows.
    assert REFRESH not in caplog.text and "at-test" not in caplog.text
    assert REFRESH not in str(g.sql("SELECT * FROM audit_log"))


def test_denied_scope_gives_no_capability(g: GoogleHarness) -> None:
    assert g.sign_in().status_code == 302
    _, cb = _connect(g, "mail", "openid email")  # the user unticked Gmail on the consent screen
    assert cb.status_code == 302
    listed = g.client.get("/api/v1/connections").json()["items"]
    assert listed[0]["capabilities"] == []
    assert g.client.get("/api/v1/me").json()["capabilities"] == []


def test_connect_state_is_bound_to_the_user(g: GoogleHarness) -> None:
    assert g.sign_in().status_code == 302
    r = g.client.post(
        "/api/v1/connections", json={"provider": "google", "capability": "mail"}, headers=g.csrf()
    )
    state = {k: v[0] for k, v in parse_qs(urlparse(r.json()["authorization_url"]).query).items()}["state"]
    bad = g.client.get(
        "/api/v1/connections/google/callback", params={"code": "c", "state": "forged"}, follow_redirects=False
    )
    assert bad.status_code in (403, 404, 422)
    assert g.sql("SELECT count(*) FROM connections")[0][0] == 0
    unknown = g.client.post(
        "/api/v1/connections", json={"provider": "google", "capability": "drive"}, headers=g.csrf()
    )
    assert unknown.status_code == 422
    assert state


def test_disconnect_revokes_and_forgets_the_token(g: GoogleHarness) -> None:
    assert g.sign_in().status_code == 302
    _connect(g, "mail", f"openid email {GMAIL_READONLY}")
    connection_id = g.client.get("/api/v1/connections").json()["items"][0]["id"]
    r = g.client.delete(f"/api/v1/connections/{connection_id}", headers=g.csrf())
    assert r.status_code == 202 and r.json() == {"status": "revoked", "purge": False, "deletion_job_id": None}
    assert g.google.revoked == [REFRESH]
    assert g.sql("SELECT status, refresh_token_ciphertext, revoked_at IS NOT NULL FROM connections") == [
        ("revoked", None, True)
    ]
    purge = g.client.delete(f"/api/v1/connections/{connection_id}?purge=true", headers=g.csrf())
    assert purge.status_code == 202 and purge.json()["deletion_job_id"]


def test_invalid_grant_marks_the_connection_needs_reauth(g: GoogleHarness) -> None:
    assert g.sign_in().status_code == 302
    _connect(g, "mail", f"openid email {GMAIL_READONLY}")
    row = g.sql("SELECT id, user_id FROM connections")[0]
    g.google.refresh_error = "invalid_grant"

    async def refresh() -> None:
        engine = create_engine(g.db.worker_url, pool_size=1, max_overflow=0)
        try:
            registry = build_connector_registry(
                g.settings, UnitOfWorkFactory(create_session_factory(engine)), http=g.http
            )
            info = ConnectionInfo(
                connection_id=UUID(str(row[0])),
                user_id=UUID(str(row[1])),
                provider="google",
                account_email=EMAIL,
            )
            connector = registry.mail(info)
            await connector._token()  # type: ignore[attr-defined]
        finally:
            await engine.dispose()

    with pytest.raises(AuthRevoked):
        asyncio.run(refresh())
    assert g.sql("SELECT status, last_error FROM connections") == [("needs_reauth", "invalid_grant")]
    assert g.sql(
        "SELECT payload->>'status' FROM outbox WHERE event_type = 'ConnectionStatusChanged' "
        "ORDER BY created_at DESC, id DESC LIMIT 1"
    ) == [("needs_reauth",)]
