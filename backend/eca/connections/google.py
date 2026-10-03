"""Google connect flow, token access, disconnect (slice 1.2, BACKEND_DESIGN.md §16.5).

- ``start_connect``: single-use ``state`` (identity's oauth state, purpose ``connect``) bound to
  the signed-in user, incremental scopes for the requested capability, offline access.
- ``complete_connect``: consume the state, exchange the code, verify the ID token's email (the
  connected account), store the refresh token envelope-encrypted and the granted scopes.
- ``access_token``: refresh on demand; ``invalid_grant`` → status ``needs_reauth`` and a
  ``ConnectionStatusChanged`` event (sync stops; the user reconnects).
- ``disconnect``: revoke at Google, delete the ciphertext, status ``revoked``.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from uuid import UUID

import httpx
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from eca.connections.events import CONNECTION_STATUS_CHANGED, ConnectionStatusChanged
from eca.connections.models import connections_table, sync_cursors_table
from eca.connections.tokens import TokenCrypto
from eca.connectors import CAPABILITY_SCOPES, refresh_access_token, revoke_token
from eca.identity import (
    JwksCache,
    authorization_url,
    consume_state,
    create_state,
    exchange_code,
    verify_id_token,
)
from eca.platform.errors import AuthRevoked, NotFound, PermissionDenied, ValidationFailed
from eca.platform.events import NewEvent
from eca.platform.ids import uuid7
from eca.platform.outbox import publish
from eca.platform.uow import UnitOfWork


@dataclass(frozen=True)
class GoogleOAuthConfig:
    client_id: str
    client_secret: str
    redirect_uri: str


@dataclass(frozen=True)
class ConnectionView:
    id: UUID
    provider: str
    account_email: str
    granted_scopes: tuple[str, ...]
    status: str
    capabilities: tuple[str, ...]


def capabilities_for(scopes: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(cap for cap, needed in CAPABILITY_SCOPES.items() if set(needed) <= set(scopes))


async def start_connect(
    uow: UnitOfWork,
    cfg: GoogleOAuthConfig,
    *,
    capability: str,
    now: datetime.datetime,
    login_hint: str | None,
) -> str:
    if capability not in CAPABILITY_SCOPES:
        raise ValidationFailed(f"unknown capability {capability!r}")
    scopes = ("openid", "email", *CAPABILITY_SCOPES[capability])
    st = await create_state(uow, purpose="connect", now=now, user_id=uow.user_id, scopes=scopes)
    return authorization_url(
        client_id=cfg.client_id,
        redirect_uri=cfg.redirect_uri,
        state=st.state,
        code_challenge=st.code_challenge,
        nonce=st.nonce,
        scopes=scopes,
        offline=True,
        login_hint=login_hint,
    )


async def complete_connect(
    uow: UnitOfWork,
    cfg: GoogleOAuthConfig,
    crypto: TokenCrypto,
    http: httpx.AsyncClient,
    jwks: JwksCache,
    *,
    state: str,
    code: str,
    now: datetime.datetime,
) -> ConnectionView:
    consumed = await consume_state(uow, state, purpose="connect", now=now)
    if consumed.user_id != uow.user_id:
        raise PermissionDenied("connect state belongs to another session")
    tokens = await exchange_code(
        http,
        client_id=cfg.client_id,
        client_secret=cfg.client_secret,
        code=code,
        code_verifier=consumed.code_verifier,
        redirect_uri=cfg.redirect_uri,
    )
    if not tokens.id_token:
        raise ValidationFailed("Google did not return an ID token for the connected account")
    account = await verify_id_token(tokens.id_token, client_id=cfg.client_id, nonce=consumed.nonce, jwks=jwks)
    t = connections_table
    existing = (
        await uow.session.execute(
            select(t.c.id, t.c.refresh_token_ciphertext, t.c.granted_scopes).where(
                t.c.provider == "google", t.c.account_email == account.email
            )
        )
    ).one_or_none()
    connection_id = existing.id if existing else uuid7()
    granted = tuple(sorted(set(tokens.scope) | set(existing.granted_scopes if existing else ())))
    if tokens.refresh_token:
        blob = crypto.encrypt(tokens.refresh_token, connection_id=connection_id)
    elif existing and existing.refresh_token_ciphertext:
        blob = bytes(existing.refresh_token_ciphertext)  # incremental grant without a new refresh token
    else:
        raise ValidationFailed("Google returned no refresh token; reconnect with consent")
    await uow.session.execute(
        insert(t)
        .values(
            id=connection_id,
            user_id=uow.user_id,
            provider="google",
            account_email=account.email,
            granted_scopes=list(granted),
            refresh_token_ciphertext=blob,
            token_key_version=crypto.version,
            status="active",
            last_error=None,
            revoked_at=None,
        )
        .on_conflict_do_update(
            constraint="ux_connections_account",
            set_={
                "granted_scopes": list(granted),
                "refresh_token_ciphertext": blob,
                "token_key_version": crypto.version,
                "status": "active",
                "last_error": None,
                "revoked_at": None,
            },
        )
    )
    await _status_event(uow, connection_id, "active")
    return ConnectionView(
        connection_id, "google", account.email, granted, "active", capabilities_for(granted)
    )


async def _status_event(uow: UnitOfWork, connection_id: UUID, status: str) -> None:
    await publish(
        uow,
        NewEvent(
            CONNECTION_STATUS_CHANGED,
            "connection",
            connection_id,
            ConnectionStatusChanged(connection_id=connection_id, status=status),
        ),
    )


async def list_connections(uow: UnitOfWork) -> list[ConnectionView]:
    t = connections_table
    rows = await uow.session.execute(
        select(t.c.id, t.c.provider, t.c.account_email, t.c.granted_scopes, t.c.status).order_by(
            t.c.created_at
        )
    )
    return [
        ConnectionView(
            r.id,
            r.provider,
            r.account_email,
            tuple(r.granted_scopes),
            r.status,
            capabilities_for(tuple(r.granted_scopes)),
        )
        for r in rows
    ]


async def access_token(
    uow: UnitOfWork,
    cfg: GoogleOAuthConfig,
    crypto: TokenCrypto,
    http: httpx.AsyncClient,
    *,
    connection_id: UUID,
    capability: str,
) -> str:
    """A fresh access token. Raises ``AuthRevoked`` on ``invalid_grant``; the caller then records it
    with :func:`mark_needs_reauth` in a new transaction."""
    t = connections_table
    row = (
        await uow.session.execute(
            select(t.c.refresh_token_ciphertext, t.c.granted_scopes, t.c.status).where(
                t.c.id == connection_id
            )
        )
    ).one_or_none()
    if row is None:
        raise NotFound("connection not found")
    if row.status != "active" or row.refresh_token_ciphertext is None:
        raise AuthRevoked(f"connection is {row.status}")
    if capability not in capabilities_for(tuple(row.granted_scopes)):
        raise PermissionDenied(f"capability {capability!r} not granted")
    refresh = crypto.decrypt(bytes(row.refresh_token_ciphertext), connection_id=connection_id)
    token = await refresh_access_token(
        http, client_id=cfg.client_id, client_secret=cfg.client_secret, refresh_token=refresh
    )
    return token.token


async def mark_needs_reauth(uow: UnitOfWork, connection_id: UUID) -> None:
    """``invalid_grant``: the connection needs a new consent (TECHNICAL_DESIGN.md §17.1). Called in
    its own transaction, because the failing token request's transaction rolls back."""
    t = connections_table
    changed = (
        await uow.session.execute(
            update(t)
            .where(t.c.id == connection_id, t.c.status == "active")
            .values(status="needs_reauth", last_error="invalid_grant")
            .returning(t.c.id)
        )
    ).scalar_one_or_none()
    if changed is not None:
        await _status_event(uow, connection_id, "needs_reauth")


async def disconnect(
    uow: UnitOfWork,
    crypto: TokenCrypto,
    http: httpx.AsyncClient,
    *,
    connection_id: UUID,
    now: datetime.datetime,
) -> None:
    t = connections_table
    row = (
        await uow.session.execute(
            select(t.c.refresh_token_ciphertext, t.c.status).where(t.c.id == connection_id)
        )
    ).one_or_none()
    if row is None:
        raise NotFound("connection not found")
    if row.refresh_token_ciphertext is not None:
        await revoke_token(
            http, crypto.decrypt(bytes(row.refresh_token_ciphertext), connection_id=connection_id)
        )
    await uow.session.execute(
        update(t)
        .where(t.c.id == connection_id)
        .values(status="revoked", refresh_token_ciphertext=None, revoked_at=now)
    )
    await _status_event(uow, connection_id, "revoked")


@dataclass(frozen=True)
class SyncState:
    """One connection's state for the coverage block (CONTEXT_ARCHITECTURE.md §9.4, §9.10)."""

    connection_id: UUID
    provider: str
    account_email: str
    status: str  # active | paused | needs_reauth | revoked | error
    capabilities: tuple[str, ...]  # mail | calendar
    last_success: dict[str, datetime.datetime | None]  # resource -> last successful sync


async def sync_states(uow: UnitOfWork) -> list[SyncState]:
    """Every connection of the user with its capabilities and last successful sync per resource.

    Google capabilities come from the granted scopes (a revoked calendar scope removes
    ``calendar``); other providers (the fake connector) have the resources they have cursors for.
    """
    t, c = connections_table, sync_cursors_table
    conns = (
        await uow.session.execute(
            select(t.c.id, t.c.provider, t.c.account_email, t.c.granted_scopes, t.c.status)
            .where(t.c.user_id == uow.user_id)
            .order_by(t.c.created_at, t.c.id)
        )
    ).all()
    cursors: dict[UUID, dict[str, datetime.datetime | None]] = {}
    for cur in await uow.session.execute(
        select(c.c.connection_id, c.c.resource, c.c.last_success_at).where(c.c.user_id == uow.user_id)
    ):
        cursors.setdefault(cur.connection_id, {})[cur.resource] = cur.last_success_at
    out = []
    for r in conns:
        resources = cursors.get(r.id, {})
        if r.provider == "google":
            caps = capabilities_for(tuple(r.granted_scopes))
        else:
            caps = tuple(sorted(resources))
        out.append(SyncState(r.id, r.provider, r.account_email, r.status, caps, dict(resources)))
    return out
