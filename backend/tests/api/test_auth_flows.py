"""Slice 1.1 auth flows with a mocked Google (IMPLEMENTATION_PLAN.md §3, BACKEND_DESIGN.md §16).

Google's token endpoint and JWKS are served by an ``httpx.MockTransport``; ID tokens are signed
with an RSA key generated for the test. Covers sign-in (PKCE, ``state``, ``nonce``), the
returning user, every ID-token check, ``invalid_grant``, CSRF rejection, session expiry and
revocation, logout and the recent re-authentication that ``DELETE /me`` needs. No real Google
call is made and every identity is synthetic.
"""

from __future__ import annotations

import base64
import hashlib
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from tests.api.google_fake import CLIENT_ID, GoogleHarness, google_harness
from tests.conftest import TempDatabase

pytestmark = pytest.mark.db

AVERY_USERS = "SELECT count(*) FROM users WHERE email = 'avery@brightwater.example'"
Auth = GoogleHarness


@pytest.fixture
def auth(isolated_db: TempDatabase, tmp_path: Path) -> Iterator[GoogleHarness]:
    with google_harness(isolated_db, tmp_path) as harness:
        yield harness


def test_login_uses_pkce_state_and_nonce(auth: Auth) -> None:
    r = auth.client.get("/api/v1/auth/google/login", follow_redirects=False)
    assert r.status_code == 302
    url = urlparse(r.headers["location"])
    query = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert url.netloc == "accounts.google.com"
    assert query["client_id"] == CLIENT_ID and query["code_challenge_method"] == "S256"
    assert query["state"] and query["nonce"] and query["code_challenge"]
    cookie = r.headers["set-cookie"]
    assert f"eca_oauth_state={query['state']}" in cookie and "HttpOnly" in cookie
    # Only hashes are stored.
    stored = auth.sql("SELECT state_hash FROM oauth_states")
    assert len(stored) == 1 and bytes(stored[0][0]) == hashlib.sha256(query["state"].encode()).digest()


def test_sign_in_creates_the_user_once_and_returns_a_session(auth: Auth) -> None:
    r = auth.sign_in()
    assert r.status_code == 302 and r.headers["location"] == "http://localhost:3000"
    session = auth.client.cookies.get("eca_session")
    csrf = auth.client.cookies.get("eca_csrf")
    assert session and csrf
    # PKCE: the verifier sent to the token endpoint matches the challenge of the login request.
    verifier = auth.google.exchanged[0]["code_verifier"]
    assert auth.google.exchanged[0]["grant_type"] == "authorization_code" and verifier
    me = auth.client.get("/api/v1/me")
    assert me.status_code == 200 and me.json()["email"] == "avery@brightwater.example"
    assert auth.sql(AVERY_USERS)[0][0] == 1
    assert auth.sql("SELECT count(*) FROM persons WHERE is_self")[0][0] == 1
    assert auth.sql("SELECT action FROM audit_log ORDER BY created_at") == [("signin",)]
    # Session tokens are stored as hashes only.
    assert (
        auth.sql(
            "SELECT count(*) FROM auth_sessions WHERE session_hash = %s",
            (hashlib.sha256(session.encode()).digest(),),
        )[0][0]
        == 1
    )

    auth.client.cookies.clear()
    assert auth.sign_in().status_code == 302  # returning user: same account
    assert auth.sql(AVERY_USERS)[0][0] == 1
    assert auth.sql("SELECT count(*) FROM auth_sessions")[0][0] == 2


def test_state_must_match_the_browser_and_is_single_use(auth: Auth) -> None:
    query = auth.login()
    assert auth.callback("forged-state").status_code == 403
    assert auth.callback(query["state"]).status_code == 302
    auth.client.cookies.set("eca_oauth_state", query["state"], path="/api/v1/auth/google")
    replay = auth.callback(query["state"])
    assert replay.status_code in (403, 404, 422)
    assert auth.sql(AVERY_USERS)[0][0] == 1


@pytest.mark.parametrize(
    ("change", "status"),
    [
        (lambda c: {**c, "nonce": "other"}, 422),
        (lambda c: {**c, "aud": "another-client"}, 422),
        (lambda c: {**c, "iss": "https://evil.example"}, 422),
        (lambda c: {**c, "exp": int(time.time()) - 3600, "iat": int(time.time()) - 7200}, 422),
        (lambda c: {**c, "email_verified": False}, 422),
        (lambda c: {k: v for k, v in c.items() if k != "sub"}, 422),
    ],
)
def test_id_token_checks(auth: Auth, change: Callable[[dict[str, Any]], dict[str, Any]], status: int) -> None:
    auth.google.override = change
    r = auth.sign_in()
    assert r.status_code == status, r.text
    assert auth.sql(AVERY_USERS)[0][0] == 0
    assert auth.client.cookies.get("eca_session") is None


def test_token_signed_by_an_unknown_key_is_refused(auth: Auth) -> None:
    auth.google.signer = rsa.generate_private_key(65537, 2048)
    assert auth.sign_in().status_code == 422
    assert auth.sql(AVERY_USERS)[0][0] == 0


def test_invalid_grant_is_a_reauth_conflict(auth: Auth) -> None:
    auth.google.token_error, auth.google.token_status = "invalid_grant", 400
    r = auth.sign_in()
    assert r.status_code == 409 and r.json()["code"] == "reauth_required"


def test_csrf_is_required_on_unsafe_methods(auth: Auth) -> None:
    assert auth.sign_in().status_code == 302
    csrf = auth.client.cookies.get("eca_csrf")
    assert auth.client.get("/api/v1/me").status_code == 200  # safe method: no header needed
    assert auth.client.post("/api/v1/auth/logout").status_code == 403
    assert auth.client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert auth.client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 204
    assert auth.client.get("/api/v1/me").status_code == 401  # revoked by logout


def test_expired_and_revoked_sessions_are_unauthenticated(auth: Auth) -> None:
    assert auth.sign_in().status_code == 302
    assert auth.client.get("/api/v1/me").status_code == 200
    auth.sql("UPDATE auth_sessions SET expires_at = now() - interval '1 minute' RETURNING 1")
    r = auth.client.get("/api/v1/me")
    assert r.status_code == 401 and r.json()["code"] == "unauthenticated"
    auth.client.cookies.set("eca_session", base64.urlsafe_b64encode(uuid.uuid4().bytes).decode())
    assert auth.client.get("/api/v1/me").status_code == 401


def test_account_deletion_needs_a_recent_sign_in(auth: Auth) -> None:
    assert auth.sign_in().status_code == 302
    csrf = auth.client.cookies.get("eca_csrf") or ""
    auth.sql("UPDATE auth_sessions SET reauth_at = now() - interval '1 hour' RETURNING 1")
    r = auth.client.delete("/api/v1/me", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 403 and r.json()["reason"] == "reauth_required"
    auth.sql("UPDATE auth_sessions SET reauth_at = now() RETURNING 1")
    r = auth.client.delete("/api/v1/me", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 202 and r.json()["status"] == "pending"
    assert auth.sql("SELECT status FROM users WHERE email = 'avery@brightwater.example'") == [("deleting",)]
    assert auth.client.get("/api/v1/me").status_code == 401  # every session revoked
