"""Server-side sessions and CSRF (slice 1.1, BACKEND_DESIGN.md §16).

The browser holds an opaque random session token (HttpOnly cookie) and a CSRF token (readable
cookie, echoed in ``X-CSRF-Token`` on unsafe methods: double submit bound to the session). Only
SHA-256 hashes are stored. Resolving a token to a user uses ``eca_session_user_id`` (the API's
only pre-authentication lookup, migration 0009); everything after runs under the user's RLS.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Column, DateTime, MetaData, Table, Text, insert, select, text, update
from sqlalchemy.dialects.postgresql import BYTEA
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from eca.platform.errors import PermissionDenied
from eca.platform.ids import uuid7
from eca.platform.uow import UnitOfWork

metadata = MetaData()
auth_sessions_table = Table(
    "auth_sessions",
    metadata,
    Column("id", PG_UUID(as_uuid=True), primary_key=True),
    Column("user_id", PG_UUID(as_uuid=True), nullable=False),
    Column("session_hash", BYTEA, nullable=False),
    Column("csrf_hash", BYTEA, nullable=False),
    Column("created_at", DateTime(timezone=True)),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("last_seen_at", DateTime(timezone=True)),
    Column("reauth_at", DateTime(timezone=True)),
    Column("ip", Text),
    Column("user_agent", Text),
    Column("revoked_at", DateTime(timezone=True)),
)


@dataclass(frozen=True)
class NewSession:
    session_id: UUID
    token: str
    csrf_token: str
    expires_at: datetime.datetime


@dataclass(frozen=True)
class SessionInfo:
    session_id: UUID
    user_id: UUID
    csrf_hash: bytes
    reauth_at: datetime.datetime


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


async def create_session(
    uow: UnitOfWork,
    *,
    ttl: datetime.timedelta,
    now: datetime.datetime,
    ip: str | None,
    user_agent: str | None,
) -> NewSession:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    session_id = uuid7()
    expires = now + ttl
    await uow.session.execute(
        insert(auth_sessions_table).values(
            id=session_id,
            user_id=uow.user_id,
            session_hash=token_hash(token),
            csrf_hash=token_hash(csrf),
            expires_at=expires,
            reauth_at=now,
            ip=ip,
            user_agent=(user_agent or "")[:300] or None,
        )
    )
    return NewSession(session_id, token, csrf, expires)


async def session_user_id(uow: UnitOfWork, token: str) -> UUID | None:
    """Pre-authentication lookup (unit of work without a user)."""
    result = await uow.session.execute(text("SELECT eca_session_user_id(:h)"), {"h": token_hash(token)})
    value = result.scalar_one_or_none()
    return value if isinstance(value, UUID) else (UUID(str(value)) if value else None)


async def load_session(uow: UnitOfWork, token: str, *, now: datetime.datetime) -> SessionInfo | None:
    """In the user's unit of work: the active session row for this token."""
    t = auth_sessions_table
    row = (
        await uow.session.execute(
            select(t.c.id, t.c.user_id, t.c.csrf_hash, t.c.reauth_at).where(
                t.c.session_hash == token_hash(token), t.c.revoked_at.is_(None), t.c.expires_at > now
            )
        )
    ).one_or_none()
    if row is None:
        return None
    await uow.session.execute(update(t).where(t.c.id == row.id).values(last_seen_at=now))
    return SessionInfo(row.id, row.user_id, bytes(row.csrf_hash), row.reauth_at)


def check_csrf(info: SessionInfo, header_value: str | None) -> None:
    if not header_value or not hmac.compare_digest(token_hash(header_value), info.csrf_hash):
        raise PermissionDenied("missing or invalid CSRF token")


async def revoke_session(uow: UnitOfWork, session_id: UUID, *, now: datetime.datetime) -> None:
    t = auth_sessions_table
    await uow.session.execute(update(t).where(t.c.id == session_id).values(revoked_at=now))


async def revoke_all_sessions(uow: UnitOfWork, *, now: datetime.datetime) -> None:
    t = auth_sessions_table
    await uow.session.execute(update(t).where(t.c.revoked_at.is_(None)).values(revoked_at=now))
