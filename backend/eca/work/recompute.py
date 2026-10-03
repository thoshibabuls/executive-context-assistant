"""Recomputation without AI (BACKEND_DESIGN.md §8.5, AI_PIPELINE.md §10).

R1 re-fold: recompute every item projection from ``context_events``.
R2 re-apply: per user under the merge lock, keep SOURCE, USER-AUTHORED events and every item or
decision the user touched; delete model and system events, evidence links of AI-derived state and
AI-only items and decisions; re-apply every succeeded email and meeting extraction in source order
(evidence and decision IDs are deterministic, so they are recreated with the same IDs); then R1.
No model call.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from sqlalchemy import delete, func, select, text, update

from eca.ingestion import set_stage, source_times
from eca.intelligence import reset_apply, succeeded_extractions
from eca.platform.feedback import feedback_events_table
from eca.platform.uow import UnitOfWork
from eca.work.apply import MERGE_LOCK_SQL, apply_extraction
from eca.work.meeting_apply import apply_meeting_extraction
from eca.work.models import (
    context_events_table,
    decisions_table,
    evidence_table,
    item_evidence_table,
    work_items_table,
)
from eca.work.service import kept_item_links, refold

REAPPLIED = ("email_extract", "meeting_extract")


@dataclass(frozen=True)
class RecomputeReport:
    items: int
    extractions: int = 0
    removed_items: int = 0


async def refold_all(uow: UnitOfWork) -> RecomputeReport:
    """R1. Idempotent; no version bump when nothing changed is not attempted (versions advance)."""
    t = work_items_table
    ids = [
        r.id
        for r in await uow.session.execute(
            select(t.c.id).where(t.c.merged_into_id.is_(None)).order_by(t.c.id)
        )
    ]
    for item_id in ids:
        await uow.session.execute(select(t.c.id).where(t.c.id == item_id).with_for_update())
        await refold(uow, item_id)
    return RecomputeReport(items=len(ids))


async def reapply_all(uow: UnitOfWork, *, now: datetime.datetime) -> RecomputeReport:
    """R2 for the unit of work's user (one transaction, BACKEND_DESIGN.md §8.5).

    Kept: SOURCE, USER-AUTHORED events, every item with a user event or ``origin = user`` and
    every decision the user touched (``origin = user``, user-edited fields, or a user correction
    recorded in ``feedback_events``). Deleted: model, system and time events, AI evidence and its
    links, AI-only items and decisions. Then every succeeded email and meeting extraction is
    re-applied in source order (evidence and decision IDs are deterministic, so kept rows are
    found again), and every item is re-folded.
    """
    await uow.session.execute(MERGE_LOCK_SQL, {"user_id": str(uow.user_id)})
    ce, wi, ie, ev, dc, fb = (
        context_events_table,
        work_items_table,
        item_evidence_table,
        evidence_table,
        decisions_table,
        feedback_events_table,
    )
    user_touched = select(ce.c.entity_id).where(ce.c.actor == "user", ce.c.entity_type == "work_item")
    keep = select(wi.c.id).where((wi.c.origin == "user") | wi.c.id.in_(user_touched))
    ai_only = [r.id for r in await uow.session.execute(select(wi.c.id).where(wi.c.id.not_in(keep)))]
    decision_feedback = select(fb.c.target_id).where(fb.c.target_type == "decision")
    keep_decisions = select(dc.c.id).where(
        (dc.c.origin == "user") | (func.cardinality(dc.c.user_fields) > 0) | dc.c.id.in_(decision_feedback)
    )
    ai_decisions = [
        r.id for r in await uow.session.execute(select(dc.c.id).where(dc.c.id.not_in(keep_decisions)))
    ]
    kept_items = [r.id for r in await uow.session.execute(keep)]
    relink = await kept_item_links(uow, kept_items)
    await uow.session.execute(delete(ce).where(ce.c.actor.in_(["model", "system", "time"])))
    ai_evidence = select(ev.c.id).where(ev.c.extraction_id.is_not(None))
    await uow.session.execute(delete(ie).where(ie.c.evidence_id.in_(ai_evidence)))
    await uow.session.execute(delete(ie).where(ie.c.item_type == "work_item", ie.c.item_id.in_(ai_only)))
    await uow.session.execute(delete(ie).where(ie.c.item_type == "decision", ie.c.item_id.in_(ai_decisions)))
    await uow.session.execute(update(wi).values(reported_status_evidence_id=None))
    if ai_decisions:
        await uow.session.execute(delete(dc).where(dc.c.id.in_(ai_decisions)))
    if ai_only:
        await uow.session.execute(delete(wi).where(wi.c.id.in_(ai_only)))
    await uow.session.execute(text("DELETE FROM entity_mentions WHERE method = 'extraction'"))
    await uow.session.execute(delete(ev).where(ev.c.extraction_id.is_not(None)))
    # Kept items: refold from their remaining (user) events, re-link happens through re-apply.
    for item_id in [r.id for r in await uow.session.execute(select(wi.c.id))]:
        await refold(uow, item_id)
    await reset_apply(uow)
    extractions = [e for e in await succeeded_extractions(uow) if e.pipeline in REAPPLIED]
    occurred = await source_times(uow, [e.source_item_id for e in extractions])
    epoch = datetime.datetime.min.replace(tzinfo=datetime.UTC)
    extractions.sort(
        key=lambda e: (occurred.get(e.source_item_id) or epoch, e.created_at or epoch, str(e.id))
    )
    for ext in extractions:
        if ext.pipeline == "email_extract":
            await set_stage(uow, ext.source_item_id, expected=("applied",), new="extracted")
            await apply_extraction(uow, ext.id, now=now, relink=relink)
        else:
            await apply_meeting_extraction(uow, ext.id, now=now, relink=relink)
    await refold_all(uow)
    return RecomputeReport(items=len(ai_only), extractions=len(extractions), removed_items=len(ai_only))
