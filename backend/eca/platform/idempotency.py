"""``Idempotency-Key`` for creating POSTs (BACKEND_DESIGN.md §16.2).

The key row is inserted in the request's own transaction: a concurrent request with the same key
waits on the primary key until the first commits, then replays its stored response. The same key
with a different request body is ``422`` (§16.3); one still in progress is ``409``.
Keys expire after 24 hours.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Column, DateTime, MetaData, SmallInteger, Table, Text, delete, select, update
from sqlalchemy.dialects.postgresql import BYTEA, JSONB, insert
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from eca.platform.errors import Conflict, ValidationFailed
from eca.platform.uow import UnitOfWork

KEY_TTL = datetime.timedelta(hours=24)
MAX_KEY_LENGTH = 255

metadata = MetaData()

idempotency_keys_table = Table(
    "idempotency_keys",
    metadata,
    Column("user_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("key", Text, primary_key=True),
    Column("request_hash", BYTEA, nullable=False),
    Column("status_code", SmallInteger),
    Column("response", JSONB),
    Column("created_at", DateTime(timezone=True)),
    Column("expires_at", DateTime(timezone=True), nullable=False),
)


@dataclass(frozen=True)
class StoredResponse:
    status_code: int
    body: dict[str, Any]


def request_hash(method: str, path: str, body: Any) -> bytes:
    canonical = json.dumps([method, path, body], sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).digest()


async def claim_key(
    uow: UnitOfWork, key: str, digest: bytes, *, now: datetime.datetime
) -> StoredResponse | None:
    """None: first use, the caller proceeds and calls ``store_response``; else the stored reply."""
    if not key or len(key) > MAX_KEY_LENGTH:
        raise ValidationFailed("Idempotency-Key must be 1-255 characters")
    t = idempotency_keys_table
    await uow.session.execute(delete(t).where(t.c.key == key, t.c.expires_at <= now))
    inserted = (
        await uow.session.execute(
            insert(t)
            .values(user_id=uow.user_id, key=key, request_hash=digest, expires_at=now + KEY_TTL)
            .on_conflict_do_nothing()
            .returning(t.c.key)
        )
    ).first()
    if inserted is not None:
        return None
    row = (
        await uow.session.execute(
            select(t.c.request_hash, t.c.status_code, t.c.response).where(t.c.key == key)
        )
    ).one()
    if bytes(row.request_hash) != digest:
        raise ValidationFailed(
            "Idempotency-Key was used with a different request", details={"reason": "key_reused"}
        )
    if row.status_code is None:
        raise Conflict(
            "a request with this Idempotency-Key is in progress", details={"reason": "in_progress"}
        )
    return StoredResponse(int(row.status_code), dict(row.response or {}))


async def store_response(uow: UnitOfWork, key: str, *, status_code: int, body: dict[str, Any]) -> None:
    t = idempotency_keys_table
    await uow.session.execute(update(t).where(t.c.key == key).values(status_code=status_code, response=body))


async def purge_expired_keys(uow: UnitOfWork, *, now: datetime.datetime) -> int:
    t = idempotency_keys_table
    result = await uow.session.execute(delete(t).where(t.c.expires_at <= now))
    return int(result.rowcount)  # type: ignore[attr-defined]


async def purge_user_keys(uow: UnitOfWork) -> None:
    await uow.session.execute(delete(idempotency_keys_table))


async def release_key(uow: UnitOfWork, key: str) -> None:
    """Forget a key whose run failed before storing a response, so the client can retry (Phase 2
    chat: the answer streams over several transactions, BACKEND_DESIGN.md §16.7)."""
    t = idempotency_keys_table
    await uow.session.execute(delete(t).where(t.c.key == key, t.c.status_code.is_(None)))
