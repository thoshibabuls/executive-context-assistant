"""Google OAuth token operations for connections (slice 1.2): refresh and revoke.

``invalid_grant`` on refresh raises ``AuthRevoked`` (the connection becomes ``needs_reauth``).
Access tokens live in memory only; refresh tokens are never logged.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from eca.platform.errors import AuthRevoked, UpstreamUnavailable

TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"  # noqa: S105 (a URL, not a secret)
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
GMAIL_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
CALENDAR_READONLY = "https://www.googleapis.com/auth/calendar.readonly"
CAPABILITY_SCOPES = {"mail": (GMAIL_READONLY,), "calendar": (CALENDAR_READONLY,)}


@dataclass(frozen=True)
class AccessToken:
    token: str
    expires_in: int
    scopes: tuple[str, ...]


async def refresh_access_token(
    http: httpx.AsyncClient, *, client_id: str, client_secret: str, refresh_token: str
) -> AccessToken:
    try:
        resp = await http.post(
            TOKEN_ENDPOINT,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=15,
        )
    except httpx.HTTPError as exc:
        raise UpstreamUnavailable("Google token endpoint unreachable") from exc
    if resp.status_code in (400, 401) and resp.json().get("error") == "invalid_grant":
        raise AuthRevoked("refresh token revoked or expired")
    if resp.status_code != 200:
        raise UpstreamUnavailable(f"Google token endpoint returned {resp.status_code}")
    body = resp.json()
    return AccessToken(
        body["access_token"], int(body.get("expires_in", 3600)), tuple(str(body.get("scope", "")).split())
    )


async def revoke_token(http: httpx.AsyncClient, token: str) -> bool:
    """Best effort: True when Google confirmed (200) or the token was already invalid (400)."""
    try:
        resp = await http.post(REVOKE_ENDPOINT, data={"token": token}, timeout=15)
    except httpx.HTTPError:
        return False
    return resp.status_code in (200, 400)
