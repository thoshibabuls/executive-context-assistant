"""Single-use OAuth ``state`` with PKCE verifier and nonce (BACKEND_DESIGN.md §10.2).

Only the SHA-256 of the state is stored. ``consume`` succeeds once, before expiry; a replayed or
forged state fails. Used by sign-in (identity) and connect (connections, through this module).
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import secrets
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Column, DateTime, MetaData, Table, Text, insert, text
from sqlalchemy.dialects.postgresql import ARRAY, BYTEA
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from eca.platform.errors import ValidationFailed
from eca.platform.uow import UnitOfWork

STATE_TTL = datetime.timedelta(minutes=10)

metadata = MetaData()
oauth_states_table = Table(
    "oauth_states",
    metadata,
    Column("state_hash", BYTEA, primary_key=True),
    Column("purpose", Text, nullable=False),
    Column("code_verifier", Text, nullable=False),
    Column("nonce", Text, nullable=False),
    Column("user_id", PG_UUID(as_uuid=True)),
    Column("scopes", ARRAY(Text), nullable=False),
    Column("redirect_to", Text),
    Column("created_at", DateTime(timezone=True)),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("consumed_at", DateTime(timezone=True)),
)


@dataclass(frozen=True)
class NewState:
    state: str
    code_verifier: str
    code_challenge: str
    nonce: str


@dataclass(frozen=True)
class ConsumedState:
    purpose: str
    code_verifier: str
    nonce: str
    user_id: UUID | None
    scopes: tuple[str, ...]
    redirect_to: str | None


def _hash(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


async def create_state(
    uow: UnitOfWork,
    *,
    purpose: str,
    now: datetime.datetime,
    user_id: UUID | None = None,
    scopes: tuple[str, ...] = (),
    redirect_to: str | None = None,
) -> NewState:
    # Expired states are removed here (API role only, §7.6): the table stays small without a job.
    await uow.session.execute(_EXPIRE_SQL, {"cutoff": now - STATE_TTL})
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    nonce = secrets.token_urlsafe(24)
    await uow.session.execute(
        insert(oauth_states_table).values(
            state_hash=_hash(state),
            purpose=purpose,
            code_verifier=verifier,
            nonce=nonce,
            user_id=user_id,
            scopes=list(scopes),
            redirect_to=redirect_to,
            expires_at=now + STATE_TTL,
        )
    )
    return NewState(state=state, code_verifier=verifier, code_challenge=pkce_challenge(verifier), nonce=nonce)


_EXPIRE_SQL = text("DELETE FROM oauth_states WHERE expires_at < :cutoff")


async def discard_user_states(uow: UnitOfWork, user_id: UUID) -> None:
    """Account deletion: states bound to the user go before the user row (FK, API role only)."""
    await uow.session.execute(text("DELETE FROM oauth_states WHERE user_id = :u"), {"u": user_id})


_CONSUME_SQL = text(
    """
    UPDATE oauth_states SET consumed_at = :now
     WHERE state_hash = :hash AND purpose = :purpose AND consumed_at IS NULL AND expires_at > :now
    RETURNING purpose, code_verifier, nonce, user_id, scopes, redirect_to
    """
)


async def consume_state(
    uow: UnitOfWork, state: str, *, purpose: str, now: datetime.datetime
) -> ConsumedState:
    row = (
        await uow.session.execute(_CONSUME_SQL, {"now": now, "hash": _hash(state), "purpose": purpose})
    ).one_or_none()
    if row is None:
        raise ValidationFailed("invalid, expired or already used state")
    return ConsumedState(
        row.purpose, row.code_verifier, row.nonce, row.user_id, tuple(row.scopes), row.redirect_to
    )
