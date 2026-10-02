"""Deletion in ``work`` (BACKEND_DESIGN.md §9.3, §13.3). No cascades, children before parents.

- ``on_source_deleted`` (provider deleted one message): its evidence keeps its ID but the quote
  becomes ``[source deleted]``; items without other live evidence are archived when suggested
  (AI-only) or flagged ``has_source_gap`` when confirmed or user-touched; a ``source_removed``
  system event records it.
- ``purge_sources`` (disconnect with purge): AI-only items whose evidence all comes from the
  purged sources are deleted with their events and links; user-touched items are kept with
  ``has_source_gap`` and redacted quotes; references to purged extractions are cleared.
- ``purge_user`` (account deletion): every work row of the user.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import delete, exists, select, union, update

from eca.platform.uow import UnitOfWork
from eca.work.models import (
    context_events_table,
    decisions_table,
    evidence_table,
    item_evidence_table,
    work_items_table,
)
from eca.work.service import append_event, system_dedupe_key

REDACTED = "[source deleted]"


async def _user_touched(uow: UnitOfWork, item_id: UUID) -> bool:
    ce, wi = context_events_table, work_items_table
    row = (
        await uow.session.execute(select(wi.c.origin, wi.c.verification_status).where(wi.c.id == item_id))
    ).one()
    if row.origin == "user" or row.verification_status in ("confirmed", "user_created"):
        return True
    return bool(
        (
            await uow.session.execute(select(exists().where(ce.c.entity_id == item_id, ce.c.actor == "user")))
        ).scalar_one()
    )


async def _live_evidence_left(uow: UnitOfWork, item_id: UUID, excluded_sources: Sequence[UUID]) -> bool:
    ie, ev = item_evidence_table, evidence_table
    return bool(
        (
            await uow.session.execute(
                select(
                    exists()
                    .where(
                        ie.c.item_type == "work_item", ie.c.item_id == item_id, ie.c.evidence_id == ev.c.id
                    )
                    .where(ev.c.source_item_id.not_in(list(excluded_sources)), ev.c.quote != REDACTED)
                )
            )
        ).scalar_one()
    )


async def _items_for_sources(uow: UnitOfWork, source_ids: Sequence[UUID]) -> list[UUID]:
    ie, ev = item_evidence_table, evidence_table
    rows = await uow.session.execute(
        select(ie.c.item_id)
        .join(ev, ev.c.id == ie.c.evidence_id)
        .where(ie.c.item_type == "work_item", ev.c.source_item_id.in_(list(source_ids)))
        .distinct()
    )
    return sorted(r.item_id for r in rows)


async def on_source_deleted(uow: UnitOfWork, source_item_id: UUID, *, now: datetime.datetime) -> None:
    ev, wi = evidence_table, work_items_table
    await uow.session.execute(update(ev).where(ev.c.source_item_id == source_item_id).values(quote=REDACTED))
    for item_id in await _items_for_sources(uow, [source_item_id]):
        if await _live_evidence_left(uow, item_id, [source_item_id]):
            continue
        touched = await _user_touched(uow, item_id)
        await uow.session.execute(
            update(wi)
            .where(wi.c.id == item_id)
            .values(**({"has_source_gap": True} if touched else {"archived": True}))
        )
        await append_event(
            uow,
            item_id=item_id,
            event_type="source_removed",
            actor="system",
            authority=1,
            materiality=2,
            occurred_at=now,
            dedupe_key=system_dedupe_key("source_removed", item_id, source_item_id),
            payload={"source_item_id": str(source_item_id), "archived": not touched},
        )


async def _delete_items(uow: UnitOfWork, item_ids: Sequence[UUID]) -> None:
    ids = list(item_ids)
    if not ids:
        return
    ce, ie, wi = context_events_table, item_evidence_table, work_items_table
    await uow.session.execute(update(wi).where(wi.c.merged_into_id.in_(ids)).values(merged_into_id=None))
    await uow.session.execute(delete(ce).where(ce.c.entity_type == "work_item", ce.c.entity_id.in_(ids)))
    await uow.session.execute(delete(ie).where(ie.c.item_type == "work_item", ie.c.item_id.in_(ids)))
    await uow.session.execute(delete(wi).where(wi.c.id.in_(ids)))


async def purge_sources(uow: UnitOfWork, source_ids: Sequence[UUID], extraction_ids: Sequence[UUID]) -> None:
    ev, ie, ce, wi, dc = (
        evidence_table,
        item_evidence_table,
        context_events_table,
        work_items_table,
        decisions_table,
    )
    srcs, exts = list(source_ids), list(extraction_ids)
    for item_id in await _items_for_sources(uow, srcs):
        if await _user_touched(uow, item_id):
            await uow.session.execute(update(wi).where(wi.c.id == item_id).values(has_source_gap=True))
        elif not await _live_evidence_left(uow, item_id, srcs):
            await _delete_items(uow, [item_id])
    purged_ev = select(ev.c.id).where(ev.c.source_item_id.in_(srcs))
    dec_ids = [
        r.item_id
        for r in await uow.session.execute(
            select(ie.c.item_id).where(ie.c.item_type == "decision", ie.c.evidence_id.in_(purged_ev))
        )
    ]
    await uow.session.execute(delete(ie).where(ie.c.item_type == "decision", ie.c.item_id.in_(dec_ids)))
    await uow.session.execute(delete(dc).where(dc.c.id.in_(dec_ids)))
    await uow.session.execute(update(ev).where(ev.c.source_item_id.in_(srcs)).values(quote=REDACTED))
    if exts:
        await uow.session.execute(update(ev).where(ev.c.extraction_id.in_(exts)).values(extraction_id=None))
        await uow.session.execute(update(ce).where(ce.c.extraction_id.in_(exts)).values(extraction_id=None))
        await uow.session.execute(update(wi).where(wi.c.extraction_id.in_(exts)).values(extraction_id=None))
        await uow.session.execute(update(dc).where(dc.c.extraction_id.in_(exts)).values(extraction_id=None))
    await delete_unreferenced_evidence(uow, srcs)


async def delete_unreferenced_evidence(uow: UnitOfWork, source_ids: Sequence[UUID]) -> None:
    """Evidence of these sources that no item link, event or reported status points at."""
    ev, ie, ce, wi = evidence_table, item_evidence_table, context_events_table, work_items_table
    referenced = union(
        select(ie.c.evidence_id),
        select(ce.c.evidence_id).where(ce.c.evidence_id.is_not(None)),
        select(wi.c.reported_status_evidence_id).where(wi.c.reported_status_evidence_id.is_not(None)),
    )
    await uow.session.execute(
        delete(ev).where(ev.c.source_item_id.in_(list(source_ids)), ev.c.id.not_in(referenced))
    )


async def detach_conversations(uow: UnitOfWork, conversation_ids: Sequence[UUID]) -> None:
    """Kept decisions lose their link to conversations that a source purge deletes."""
    dc = decisions_table
    if conversation_ids:
        await uow.session.execute(
            update(dc).where(dc.c.conversation_id.in_(list(conversation_ids))).values(conversation_id=None)
        )


async def purge_user(uow: UnitOfWork) -> None:
    ce, ie, ev, wi, dc = (
        context_events_table,
        item_evidence_table,
        evidence_table,
        work_items_table,
        decisions_table,
    )
    await uow.session.execute(delete(ce))
    await uow.session.execute(delete(ie))
    await uow.session.execute(update(wi).values(merged_into_id=None, reported_status_evidence_id=None))
    await uow.session.execute(delete(wi))
    await uow.session.execute(update(dc).values(merged_into_id=None))
    await uow.session.execute(delete(dc))
    await uow.session.execute(delete(ev))


async def sources_still_referenced(uow: UnitOfWork, source_ids: Sequence[UUID]) -> set[UUID]:
    """Sources kept as tombstones because redacted evidence of a user-touched item points at them."""
    ev = evidence_table
    rows = await uow.session.execute(
        select(ev.c.source_item_id).where(ev.c.source_item_id.in_(list(source_ids))).distinct()
    )
    return {r.source_item_id for r in rows}
