"""Recomputation without AI (BACKEND_DESIGN.md §8.5, AI_PIPELINE.md §10).

R1 re-fold: recompute every item projection from ``context_events``.
R2 re-apply: per user under the merge lock, keep SOURCE, USER-AUTHORED events and every item with
a user event or ``origin = user``; delete model and system events, evidence links of AI-derived
state and AI-only items and decisions; re-apply every succeeded extraction in order (evidence
IDs are deterministic, so they are recreated with the same IDs); then R1. No model call.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from sqlalchemy import delete, select, text, update

from eca.ingestion import set_stage
from eca.intelligence import reset_apply, succeeded_extractions
from eca.platform.uow import UnitOfWork
from eca.work.apply import MERGE_LOCK_SQL, apply_extraction
from eca.work.models import (
    context_events_table,
    decisions_table,
    evidence_table,
    item_evidence_table,
    work_items_table,
)
from eca.work.service import refold


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
    """R2 for the unit of work's user (one transaction)."""
    await uow.session.execute(MERGE_LOCK_SQL, {"user_id": str(uow.user_id)})
    ce, wi, ie, ev, dc = (
        context_events_table,
        work_items_table,
        item_evidence_table,
        evidence_table,
        decisions_table,
    )
    user_touched = select(ce.c.entity_id).where(ce.c.actor == "user", ce.c.entity_type == "work_item")
    keep = select(wi.c.id).where((wi.c.origin == "user") | wi.c.id.in_(user_touched))
    ai_only = [r.id for r in await uow.session.execute(select(wi.c.id).where(wi.c.id.not_in(keep)))]
    await uow.session.execute(delete(ce).where(ce.c.actor.in_(["model", "system", "time"])))
    await uow.session.execute(delete(ie).where(ie.c.item_type == "decision"))
    await uow.session.execute(delete(ie).where(ie.c.item_type == "work_item", ie.c.item_id.in_(ai_only)))
    await uow.session.execute(
        delete(ie).where(
            ie.c.item_type == "work_item",
            ie.c.evidence_id.in_(select(ev.c.id).where(ev.c.extraction_id.is_not(None))),
        )
    )
    await uow.session.execute(update(wi).where(wi.c.id.in_(ai_only)).values(reported_status_evidence_id=None))
    await uow.session.execute(delete(dc))
    if ai_only:
        await uow.session.execute(delete(wi).where(wi.c.id.in_(ai_only)))
    await uow.session.execute(text("DELETE FROM entity_mentions WHERE method = 'extraction'"))
    await uow.session.execute(update(wi).values(reported_status_evidence_id=None))
    await uow.session.execute(delete(ev).where(ev.c.extraction_id.is_not(None)))
    # Kept items: refold from their remaining (user) events, re-link happens through re-apply.
    for item_id in [r.id for r in await uow.session.execute(select(wi.c.id))]:
        await refold(uow, item_id)
    await reset_apply(uow)
    extractions = await succeeded_extractions(uow)
    for ext in extractions:
        if ext.pipeline != "email_extract":
            continue
        await set_stage(uow, ext.source_item_id, expected=("applied",), new="extracted")
        await apply_extraction(uow, ext.id, now=now)
    await refold_all(uow)
    return RecomputeReport(items=len(ai_only), extractions=len(extractions), removed_items=len(ai_only))
