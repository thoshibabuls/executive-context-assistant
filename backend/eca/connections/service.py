"""Connections and sync cursors (BACKEND_DESIGN.md §11.1).

Batch A creates connections only through ``create_connection`` (no OAuth; tokens stay NULL).
Cursor rules:
1. ``advance_cursor`` is called only after every change of the run is committed.
2. One run per (connection, resource): a row lease (``acquire_lease``); a second run while the
   lease is live gets ``None`` and does nothing (RT-07).
3. The page token is committed after each page (``save_page_token``), so a crash resumes there.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert

from eca.connections.models import connections_table, sync_cursors_table
from eca.connectors import ConnectionInfo
from eca.platform.errors import NotFound, ValidationFailed
from eca.platform.ids import uuid7
from eca.platform.uow import UnitOfWork

BATCH_A_PROVIDERS = frozenset({"fake"})


@dataclass(frozen=True)
class CursorState:
    cursor: str | None
    page_token: str | None
    import_state: str


async def create_connection(uow: UnitOfWork, *, provider: str, account_email: str) -> UUID:
    if provider not in BATCH_A_PROVIDERS:
        raise ValidationFailed(f"provider {provider!r} needs the Batch B connect flow")
    connection_id = uuid7()
    await uow.session.execute(
        insert(connections_table).values(
            id=connection_id, user_id=uow.user_id, provider=provider, account_email=account_email.lower()
        )
    )
    return connection_id


async def get_connection(uow: UnitOfWork, connection_id: UUID) -> ConnectionInfo:
    row = (
        await uow.session.execute(
            select(
                connections_table.c.id,
                connections_table.c.user_id,
                connections_table.c.provider,
                connections_table.c.account_email,
                connections_table.c.status,
            ).where(connections_table.c.id == connection_id)
        )
    ).one_or_none()
    if row is None:
        raise NotFound(f"connection {connection_id} not found")
    return ConnectionInfo(
        connection_id=row.id, user_id=row.user_id, provider=row.provider, account_email=row.account_email
    )


async def list_active_connections(uow: UnitOfWork) -> list[ConnectionInfo]:
    rows = await uow.session.execute(
        select(
            connections_table.c.id,
            connections_table.c.user_id,
            connections_table.c.provider,
            connections_table.c.account_email,
        )
        .where(connections_table.c.status == "active")
        .order_by(connections_table.c.id)
    )
    return [ConnectionInfo(r.id, r.user_id, r.provider, r.account_email) for r in rows]


_ACQUIRE_SQL = text(
    """
    UPDATE sync_cursors
       SET lease_owner = :owner, lease_expires_at = :expires, last_attempt_at = :now
     WHERE connection_id = :connection_id AND resource = :resource
       AND (lease_owner IS NULL OR lease_expires_at < :now OR lease_owner = :owner)
    RETURNING cursor, import_page_token, import_state
    """
)


async def acquire_lease(
    uow: UnitOfWork,
    *,
    connection_id: UUID,
    resource: str,
    owner: str,
    now: datetime.datetime,
    ttl: datetime.timedelta = datetime.timedelta(minutes=10),
) -> CursorState | None:
    await uow.session.execute(
        insert(sync_cursors_table)
        .values(connection_id=connection_id, resource=resource, user_id=uow.user_id)
        .on_conflict_do_nothing(index_elements=["connection_id", "resource"])
    )
    row = (
        await uow.session.execute(
            _ACQUIRE_SQL,
            {
                "owner": owner,
                "expires": now + ttl,
                "now": now,
                "connection_id": connection_id,
                "resource": resource,
            },
        )
    ).one_or_none()
    if row is None:
        return None
    return CursorState(cursor=row.cursor, page_token=row.import_page_token, import_state=row.import_state)


def _owned(connection_id: UUID, resource: str, owner: str):  # type: ignore[no-untyped-def]
    c = sync_cursors_table.c
    return (c.connection_id == connection_id) & (c.resource == resource) & (c.lease_owner == owner)


async def save_page_token(
    uow: UnitOfWork, *, connection_id: UUID, resource: str, owner: str, page_token: str | None, processed: int
) -> None:
    result = await uow.session.execute(
        update(sync_cursors_table)
        .where(_owned(connection_id, resource, owner))
        .values(
            import_page_token=page_token,
            import_state="running",
            import_processed=sync_cursors_table.c.import_processed + processed,
        )
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        raise RuntimeError("sync lease lost")


async def advance_cursor(
    uow: UnitOfWork, *, connection_id: UUID, resource: str, owner: str, cursor: str, now: datetime.datetime
) -> None:
    """End of a successful run: store the new cursor, clear the page token, release the lease."""
    result = await uow.session.execute(
        update(sync_cursors_table)
        .where(_owned(connection_id, resource, owner))
        .values(
            cursor=cursor,
            cursor_obtained_at=now,
            import_page_token=None,
            import_state="done",
            last_success_at=now,
            consecutive_failures=0,
            lease_owner=None,
            lease_expires_at=None,
        )
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        raise RuntimeError("sync lease lost")


async def release_after_failure(uow: UnitOfWork, *, connection_id: UUID, resource: str, owner: str) -> None:
    """A failed run keeps its cursor and page token; the lease is released for the next run."""
    await uow.session.execute(
        update(sync_cursors_table)
        .where(_owned(connection_id, resource, owner))
        .values(
            lease_owner=None,
            lease_expires_at=None,
            consecutive_failures=sync_cursors_table.c.consecutive_failures + 1,
        )
    )


async def reset_cursor(uow: UnitOfWork, *, connection_id: UUID, resource: str, owner: str) -> None:
    """Cursor expired at the provider: restart with a bounded full re-sync (idempotent upserts)."""
    await uow.session.execute(
        update(sync_cursors_table)
        .where(_owned(connection_id, resource, owner))
        .values(cursor=None, import_page_token=None, import_state="none")
    )


async def get_cursor(uow: UnitOfWork, *, connection_id: UUID, resource: str) -> CursorState | None:
    row = (
        await uow.session.execute(
            select(
                sync_cursors_table.c.cursor,
                sync_cursors_table.c.import_page_token,
                sync_cursors_table.c.import_state,
            ).where(
                sync_cursors_table.c.connection_id == connection_id, sync_cursors_table.c.resource == resource
            )
        )
    ).one_or_none()
    return None if row is None else CursorState(row.cursor, row.import_page_token, row.import_state)
