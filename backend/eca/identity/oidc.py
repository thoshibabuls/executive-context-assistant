"""Google OpenID Connect sign-in (slice 1.1): authorization URL with PKCE, ``state`` and
``nonce``; code exchange; ID token verification against Google's JWKS.

Tokens are never logged. The ID token's signature, issuer, audience, expiry and nonce are all
checked; an unverified email is refused.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt

from eca.platform.errors import AuthRevoked, UpstreamUnavailable, ValidationFailed

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"  # noqa: S105 (a URL, not a secret)
JWKS_URI = "https://www.googleapis.com/oauth2/v3/certs"
ISSUERS = ("https://accounts.google.com", "accounts.google.com")
SIGNIN_SCOPES = ("openid", "email", "profile")
JWKS_TTL_S = 3600


@dataclass(frozen=True)
class GoogleIdentity:
    sub: str
    email: str
    name: str | None


@dataclass(frozen=True)
class TokenResponse:
    access_token: str
    refresh_token: str | None
    id_token: str | None
    scope: tuple[str, ...]
    expires_in: int


def authorization_url(
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    nonce: str,
    scopes: tuple[str, ...] = SIGNIN_SCOPES,
    offline: bool = False,
    login_hint: str | None = None,
) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "include_granted_scopes": "true",
        "prompt": "consent" if offline else "select_account",
    }
    if offline:
        params["access_type"] = "offline"
    if login_hint:
        params["login_hint"] = login_hint
    return f"{AUTH_ENDPOINT}?{urlencode(params)}"


async def exchange_code(
    http: httpx.AsyncClient,
    *,
    client_id: str,
    client_secret: str,
    code: str,
    code_verifier: str,
    redirect_uri: str,
) -> TokenResponse:
    try:
        resp = await http.post(
            TOKEN_ENDPOINT,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "code_verifier": code_verifier,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
            timeout=15,
        )
    except httpx.HTTPError as exc:
        raise UpstreamUnavailable("Google token endpoint unreachable") from exc
    if resp.status_code == 400 and resp.json().get("error") == "invalid_grant":
        raise AuthRevoked("authorization code rejected")
    if resp.status_code != 200:
        raise UpstreamUnavailable(f"Google token endpoint returned {resp.status_code}")
    body = resp.json()
    return TokenResponse(
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token"),
        id_token=body.get("id_token"),
        scope=tuple(str(body.get("scope", "")).split()),
        expires_in=int(body.get("expires_in", 3600)),
    )


class JwksCache:
    def __init__(self, http: httpx.AsyncClient, uri: str = JWKS_URI) -> None:
        self._http = http
        self._uri = uri
        self._keys: dict[str, Any] = {}
        self._fetched_at = 0.0

    async def key(self, kid: str) -> Any:
        if kid not in self._keys or time.monotonic() - self._fetched_at > JWKS_TTL_S:
            try:
                resp = await self._http.get(self._uri, timeout=10)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise UpstreamUnavailable("Google JWKS unreachable") from exc
            self._keys = {k["kid"]: jwt.PyJWK(k).key for k in resp.json().get("keys", [])}
            self._fetched_at = time.monotonic()
        if kid not in self._keys:
            raise ValidationFailed("ID token signed with an unknown key")
        return self._keys[kid]


async def verify_id_token(id_token: str, *, client_id: str, nonce: str, jwks: JwksCache) -> GoogleIdentity:
    try:
        header = jwt.get_unverified_header(id_token)
        key = await jwks.key(str(header.get("kid")))
        claims = jwt.decode(
            id_token,
            key=key,
            algorithms=["RS256"],
            audience=client_id,
            issuer=list(ISSUERS),
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            leeway=30,
        )
    except jwt.PyJWTError as exc:
        raise ValidationFailed("invalid ID token") from exc
    if claims.get("nonce") != nonce:
        raise ValidationFailed("ID token nonce mismatch")
    if not claims.get("email") or claims.get("email_verified") is not True:
        raise ValidationFailed("Google account email is not verified")
    return GoogleIdentity(sub=str(claims["sub"]), email=str(claims["email"]).lower(), name=claims.get("name"))
