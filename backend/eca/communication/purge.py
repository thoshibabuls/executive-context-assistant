"""Deletion and retention in ``communication`` (BACKEND_DESIGN.md §9.3, §13.3; TECHNICAL_DESIGN.md §9.3).

- ``on_source_deleted``: body and clean body permanently deleted; conversation state recomputed;
  a conversation without remaining messages is deleted. No AI call.
- ``purge_bodies``: retention — bodies of prefiltered mail older than 30 days and of relevant mail
  older than 180 days are nulled (``body_purged_at``); rows, triage and evidence stay.
- ``purge_user`` / ``purge_sources``: ordered deletion (participants → messages → conversations).
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import delete, exists, select, update

from eca.communication.models import conversations_table, message_participants_table, messages_table
from eca.communication.service import publish_state_changed, recompute_reply_state
from eca.platform.uow import UnitOfWork

PREFILTERED_BODY_DAYS = 30
RELEVANT_BODY_DAYS = 180


async def on_source_deleted(uow: UnitOfWork, source_item_id: UUID, *, now: datetime.datetime) -> None:
    m, c = messages_table, conversations_table
    row = (
        await uow.session.execute(
            update(m)
            .where(m.c.source_item_id == source_item_id)
            .values(body_text=None, body_clean=None, snippet=None, body_purged_at=now, deleted_at=now)
            .returning(m.c.conversation_id)
        )
    ).one_or_none()
    if row is None:
        return
    remaining = (
        await uow.session.execute(
            select(exists().where(m.c.conversation_id == row.conversation_id, m.c.deleted_at.is_(None)))
        )
    ).scalar_one()
    if remaining:
        await recompute_reply_state(uow, row.conversation_id)
        await publish_state_changed(uow, row.conversation_id, "deleted")
    else:
        await uow.session.execute(update(c).where(c.c.id == row.conversation_id).values(deleted_at=now))


async def purge_bodies(uow: UnitOfWork, *, now: datetime.datetime) -> int:
    m = messages_table
    total = 0
    for bulk_or_skipped, days in ((True, PREFILTERED_BODY_DAYS), (False, RELEVANT_BODY_DAYS)):
        cutoff = now - datetime.timedelta(days=days)
        cond = m.c.prefilter_reason.is_not(None) if bulk_or_skipped else m.c.prefilter_reason.is_(None)
        result = await uow.session.execute(
            update(m)
            .where(cond, m.c.sent_at < cutoff, m.c.body_purged_at.is_(None))
            .values(body_text=None, body_clean=None, body_purged_at=now)
        )
        total += int(result.rowcount)  # type: ignore[attr-defined]
    return total


async def purge_sources(uow: UnitOfWork, source_item_ids: Sequence[UUID]) -> None:
    m, mp, c = messages_table, message_participants_table, conversations_table
    ids = list(source_item_ids)
    if not ids:
        return
    msg_ids = select(m.c.id).where(m.c.source_item_id.in_(ids))
    conv_ids = [
        r.conversation_id
        for r in await uow.session.execute(
            select(m.c.conversation_id).where(m.c.source_item_id.in_(ids)).distinct()
        )
    ]
    await uow.session.execute(delete(mp).where(mp.c.message_id.in_(msg_ids)))
    await uow.session.execute(delete(m).where(m.c.source_item_id.in_(ids)))
    for conv in conv_ids:
        left = (await uow.session.execute(select(exists().where(m.c.conversation_id == conv)))).scalar_one()
        if left:
            await recompute_reply_state(uow, conv)
        else:
            await uow.session.execute(delete(c).where(c.c.id == conv))


async def purge_user(uow: UnitOfWork) -> None:
    await uow.session.execute(delete(message_participants_table))
    await uow.session.execute(delete(messages_table))
    await uow.session.execute(delete(conversations_table))


async def conversation_ids_for_sources(uow: UnitOfWork, source_item_ids: Sequence[UUID]) -> list[UUID]:
    m = messages_table
    rows = await uow.session.execute(
        select(m.c.conversation_id).where(m.c.source_item_id.in_(list(source_item_ids))).distinct()
    )
    return sorted(r.conversation_id for r in rows)


async def detach_connection(uow: UnitOfWork, connection_id: UUID) -> None:
    """Conversations left after a source purge no longer point at the deleted connection."""
    c = conversations_table
    await uow.session.execute(update(c).where(c.c.connection_id == connection_id).values(connection_id=None))


async def sources_without_body(uow: UnitOfWork, source_item_ids: Sequence[UUID]) -> set[UUID]:
    """Messages among these sources whose body is no longer stored (retention, deletion): the
    retrieval index must not keep their text (CONTEXT_ARCHITECTURE.md §9.7, retention)."""
    ids = list(source_item_ids)
    if not ids:
        return set()
    m = messages_table
    rows = await uow.session.execute(
        select(m.c.source_item_id).where(
            m.c.user_id == uow.user_id,
            m.c.source_item_id.in_(ids),
            (m.c.body_purged_at.is_not(None)) | (m.c.deleted_at.is_not(None)),
        )
    )
    return {r.source_item_id for r in rows}
