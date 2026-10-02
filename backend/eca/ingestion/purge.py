"""Deletion in ``ingestion`` (BACKEND_DESIGN.md §13.3).

Source items go last among content rows. A source still referenced by redacted evidence of a
user-touched work item stays as a content-free tombstone (content, metadata and connection link
cleared, ``deleted_at`` set) so the item keeps its ``has_source_gap`` history; all others are
deleted.
"""

from __future__ import annotations

import datetime
from collections.abc import Collection, Sequence
from uuid import UUID

from sqlalchemy import delete, select, update

from eca.ingestion.models import source_items_table
from eca.platform.uow import UnitOfWork


async def source_ids_for_connection(uow: UnitOfWork, connection_id: UUID, *, limit: int) -> list[UUID]:
    """Next batch of sources still attached to the connection (tombstones are detached)."""
    s = source_items_table
    rows = await uow.session.execute(
        select(s.c.id).where(s.c.connection_id == connection_id).order_by(s.c.id).limit(limit)
    )
    return [r.id for r in rows]


async def purge_sources(
    uow: UnitOfWork, source_ids: Sequence[UUID], *, keep: Collection[UUID], now: datetime.datetime
) -> None:
    s = source_items_table
    kept = [i for i in source_ids if i in keep]
    gone = [i for i in source_ids if i not in keep]
    if kept:
        await uow.session.execute(
            update(s)
            .where(s.c.id.in_(kept))
            .values(content=None, raw_metadata={}, connection_id=None, categories=[], deleted_at=now)
        )
    if gone:
        await uow.session.execute(delete(s).where(s.c.id.in_(gone)))


async def purge_user(uow: UnitOfWork) -> None:
    await uow.session.execute(delete(source_items_table))
