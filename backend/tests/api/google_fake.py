"""A fake Google for the auth and connection tests: token endpoint (authorization code and refresh
grants), revocation and JWKS, served through ``httpx.MockTransport``. ID tokens are signed with an
RSA key generated per test. No request leaves the process; every value is synthetic."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import psycopg
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from eca.api.app import create_app
from eca.identity import JwksCache
from eca.platform.config import Settings
from tests.conftest import TempDatabase

CLIENT_ID = "test-client-id.apps.googleusercontent.example"
CLIENT_SECRET = "test-client-secret"  # test-only value
TOKEN_KEK = base64.b64encode(hashlib.sha256(b"eca-test-kek").digest()).decode()  # test-only key
KID = "test-key-1"
EMAIL = "avery@brightwater.example"


@dataclass
class FakeGoogle:
    key: rsa.RSAPrivateKey = field(default_factory=lambda: rsa.generate_private_key(65537, 2048))
    claims: dict[str, Any] = field(default_factory=dict)
    nonce: str | None = None
    token_status: int = 200
    token_error: str | None = None
    refresh_error: str | None = None
    granted_scope: str = "openid email"
    refresh_token: str | None = "rt-test-refresh-0001"
    exchanged: list[dict[str, str]] = field(default_factory=list)
    refreshed: list[dict[str, str]] = field(default_factory=list)
    revoked: list[str] = field(default_factory=list)
    override: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    signer: rsa.RSAPrivateKey | None = None  # sign with another key (unknown-key test)

    def jwks(self) -> dict[str, Any]:
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        return {"keys": [{**jwk, "kid": KID, "alg": "RS256", "use": "sig"}]}

    def id_token(self) -> str:
        now = int(time.time())
        claims = {
            "iss": "https://accounts.google.com",
            "aud": CLIENT_ID,
            "sub": "google-sub-0001",
            "email": EMAIL,
            "email_verified": True,
            "name": "Avery Lindqvist",
            "iat": now,
            "exp": now + 3600,
            "nonce": self.nonce,
            **self.claims,
        }
        if self.override is not None:
            claims = self.override(claims)
        return jwt.encode(claims, self.signer or self.key, algorithm="RS256", headers={"kid": KID})

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()} if request.content else {}
        if "certs" in url:
            return httpx.Response(200, json=self.jwks())
        if url.endswith("/revoke"):
            self.revoked.append(form.get("token", ""))
            return httpx.Response(200)
        if url.endswith("/token") and form.get("grant_type") == "refresh_token":
            self.refreshed.append(form)
            if self.refresh_error:
                return httpx.Response(400, json={"error": self.refresh_error})
            return httpx.Response(200, json={"access_token": "at-refreshed", "expires_in": 3600})
        if url.endswith("/token"):
            self.exchanged.append(form)
            if self.token_error:
                return httpx.Response(self.token_status, json={"error": self.token_error})
            body: dict[str, Any] = {
                "access_token": "at-test",
                "id_token": self.id_token(),
                "expires_in": 3600,
                "scope": self.granted_scope,
            }
            if self.refresh_token:
                body["refresh_token"] = self.refresh_token
            return httpx.Response(200, json=body)
        return httpx.Response(404)


@dataclass
class GoogleHarness:
    client: TestClient
    google: FakeGoogle
    db: TempDatabase
    settings: Settings
    http: httpx.AsyncClient

    def login(self) -> dict[str, str]:
        r = self.client.get("/api/v1/auth/google/login", follow_redirects=False)
        assert r.status_code == 302
        query = {k: v[0] for k, v in parse_qs(urlparse(r.headers["location"]).query).items()}
        self.google.nonce = query["nonce"]
        return query

    def callback(self, state: str, code: str = "code-1") -> httpx.Response:
        return self.client.get(
            "/api/v1/auth/google/callback", params={"code": code, "state": state}, follow_redirects=False
        )

    def sign_in(self) -> httpx.Response:
        return self.callback(self.login()["state"])

    def csrf(self) -> dict[str, str]:
        return {"X-CSRF-Token": self.client.cookies.get("eca_csrf") or ""}

    def sql(self, query: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        with psycopg.connect(self.db.admin_url) as conn:
            return conn.execute(query, params).fetchall()  # type: ignore[arg-type]


@contextmanager
def google_harness(db: TempDatabase, tmp_path: Path) -> Iterator[GoogleHarness]:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        api_env="test",
        api_database_url=db.runtime_url,
        google_client_id=CLIENT_ID,
        google_client_secret=CLIENT_SECRET,
        token_kek=TOKEN_KEK,
        session_cookie_secure=False,
        web_base_url="http://localhost:3000",
        api_storage_local_dir=str(tmp_path / "objects"),
    )
    google = FakeGoogle()
    app = create_app(settings)
    with TestClient(app, base_url="http://localhost:8000") as client:
        http = httpx.AsyncClient(transport=httpx.MockTransport(google.handler))
        app.state.http = http
        app.state.jwks = JwksCache(http)
        yield GoogleHarness(client, google, db, settings, http)
