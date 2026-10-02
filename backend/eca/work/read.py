"""Read side of ``work`` for retrieval, the change feed and projects (Phase 2).

Every statement carries an explicit ``user_id`` predicate in addition to RLS
(CONTEXT_ARCHITECTURE.md §9.7). Rejected, merged and deleted rows are left out unless a caller
asks for them; evidence whose quote was redacted is never returned as a quote.
"""

from __future__ import annotations

import datetime
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import Select, func, literal_column, or_, select

from eca.platform.uow import UnitOfWork
from eca.work.models import (
    context_events_table,
    decisions_table,
    evidence_table,
    item_evidence_table,
    work_items_table,
)
from eca.work.purge import REDACTED
from eca.work.queries import DecisionView, EvidenceView, _sources_by_item, decision_view
from eca.work.service import WorkItemView, _view

OPEN_STATES = ("open", "in_progress")


@dataclass(frozen=True)
class TimelineEvent:
    id: UUID
    entity_type: str
    entity_id: UUID
    event_type: str
    actor: str
    authority: int
    materiality: int
    occurred_at: datetime.datetime
    recorded_at: datetime.datetime
    payload: dict[str, Any]
    evidence_id: UUID | None


def _ts_query(text: str) -> Any:
    """``to_tsquery('english', 't1 | t2')``; the caller passes sanitized alphanumeric terms."""
    return func.to_tsquery(literal_column("'english'::regconfig"), text)


def _live_items(uow: UnitOfWork, *, include_rejected: bool = False) -> Select[Any]:
    t = work_items_table
    stmt = select(t).where(t.c.user_id == uow.user_id, t.c.merged_into_id.is_(None), t.c.deleted_at.is_(None))
    if not include_rejected:
        stmt = stmt.where(t.c.verification_status != "rejected")
    return stmt


async def items_by_ids(
    uow: UnitOfWork, item_ids: Sequence[UUID], *, include_rejected: bool = False
) -> list[WorkItemView]:
    ids = list(item_ids)
    if not ids:
        return []
    t = work_items_table
    rows = (
        await uow.session.execute(
            _live_items(uow, include_rejected=include_rejected).where(t.c.id.in_(ids)).order_by(t.c.id)
        )
    ).all()
    sources = await _sources_by_item(uow, [r.id for r in rows])
    return [_view(r, sources.get(r.id, ())) for r in rows]


async def search_items(
    uow: UnitOfWork, ts_terms: str, *, limit: int = 20, include_closed: bool = False
) -> list[tuple[WorkItemView, float]]:
    """Full-text discovery over item titles and descriptions (no item embeddings in Phase 2)."""
    if not ts_terms:
        return []
    t = work_items_table
    vector = func.to_tsvector(
        literal_column("'english'::regconfig"),
        func.coalesce(t.c.title, "") + " " + func.coalesce(t.c.description, ""),
    )
    query = _ts_query(ts_terms)
    rank = func.ts_rank_cd(vector, query, 32).label("rank")
    stmt = _live_items(uow).add_columns(rank).where(vector.op("@@")(query), ~t.c.archived)
    if not include_closed:
        stmt = stmt.where(t.c.lifecycle_status.in_(OPEN_STATES))
    rows = (await uow.session.execute(stmt.order_by(rank.desc(), t.c.id).limit(limit))).all()
    sources = await _sources_by_item(uow, [r.id for r in rows])
    return [(_view(r, sources.get(r.id, ())), float(r.rank)) for r in rows]


def _live_decisions(uow: UnitOfWork) -> Select[Any]:
    d = decisions_table
    return select(d).where(
        d.c.user_id == uow.user_id,
        d.c.merged_into_id.is_(None),
        d.c.deleted_at.is_(None),
        d.c.verification_status != "rejected",
    )


async def search_decisions(
    uow: UnitOfWork, ts_terms: str, *, limit: int = 10
) -> list[tuple[DecisionView, float]]:
    if not ts_terms:
        return []
    d = decisions_table
    vector = func.to_tsvector(literal_column("'english'::regconfig"), d.c.statement)
    query = _ts_query(ts_terms)
    rank = func.ts_rank_cd(vector, query, 32).label("rank")
    rows = (
        await uow.session.execute(
            _live_decisions(uow)
            .add_columns(rank)
            .where(vector.op("@@")(query))
            .order_by(rank.desc(), d.c.id)
            .limit(limit)
        )
    ).all()
    return [(decision_view(r), float(r.rank)) for r in rows]


async def decisions_by_ids(uow: UnitOfWork, decision_ids: Sequence[UUID]) -> list[DecisionView]:
    ids = list(decision_ids)
    if not ids:
        return []
    d = decisions_table
    rows = (await uow.session.execute(_live_decisions(uow).where(d.c.id.in_(ids)).order_by(d.c.id))).all()
    return [decision_view(r) for r in rows]


async def decisions_for_conversations(
    uow: UnitOfWork, conversation_ids: Sequence[UUID], *, limit: int = 20
) -> list[DecisionView]:
    ids = list(conversation_ids)
    if not ids:
        return []
    d = decisions_table
    rows = (
        await uow.session.execute(
            _live_decisions(uow)
            .where(d.c.conversation_id.in_(ids))
            .order_by(d.c.created_at.desc(), d.c.id)
            .limit(limit)
        )
    ).all()
    return [decision_view(r) for r in rows]


async def decisions_created_between(
    uow: UnitOfWork, start: datetime.datetime, end: datetime.datetime, *, limit: int = 100
) -> list[DecisionView]:
    """Decisions recorded in the window (decisions have no ``created`` context event yet)."""
    d = decisions_table
    rows = (
        await uow.session.execute(
            _live_decisions(uow)
            .where(d.c.created_at >= start, d.c.created_at < end)
            .order_by(d.c.created_at, d.c.id)
            .limit(limit)
        )
    ).all()
    return [decision_view(r) for r in rows]


async def item_ids_for_sources(
    uow: UnitOfWork, source_item_ids: Sequence[UUID]
) -> tuple[dict[UUID, set[UUID]], dict[UUID, set[UUID]]]:
    """(work item → sources, decision → sources) for items with evidence from these sources."""
    ids = list(source_item_ids)
    items: dict[UUID, set[UUID]] = defaultdict(set)
    decisions: dict[UUID, set[UUID]] = defaultdict(set)
    if not ids:
        return items, decisions
    ie, ev = item_evidence_table, evidence_table
    rows = await uow.session.execute(
        select(ie.c.item_type, ie.c.item_id, ev.c.source_item_id)
        .join(ev, ev.c.id == ie.c.evidence_id)
        .where(ie.c.user_id == uow.user_id, ev.c.source_item_id.in_(ids), ie.c.relation != "superseded")
    )
    for r in rows:
        (items if r.item_type == "work_item" else decisions)[r.item_id].add(r.source_item_id)
    return items, decisions


async def evidence_for(
    uow: UnitOfWork, item_type: str, item_ids: Sequence[UUID], *, per_item: int = 2
) -> dict[UUID, list[EvidenceView]]:
    """Up to ``per_item`` live (not redacted) quotes per item, newest first."""
    ids = list(item_ids)
    if not ids:
        return {}
    ie, ev = item_evidence_table, evidence_table
    rows = await uow.session.execute(
        select(
            ie.c.item_id,
            ev.c.id,
            ev.c.source_item_id,
            ev.c.quote,
            ie.c.relation,
            ev.c.extraction_id,
            ev.c.occurred_at,
        )
        .join(ev, ev.c.id == ie.c.evidence_id)
        .where(
            ie.c.user_id == uow.user_id,
            ie.c.item_type == item_type,
            ie.c.item_id.in_(ids),
            ie.c.relation != "superseded",
            ev.c.quote != REDACTED,
        )
        .order_by(ie.c.item_id, ev.c.occurred_at.desc(), ev.c.id)
    )
    out: dict[UUID, list[EvidenceView]] = defaultdict(list)
    for r in rows:
        if len(out[r.item_id]) < per_item:
            out[r.item_id].append(
                EvidenceView(r.id, r.source_item_id, r.quote, r.relation, r.extraction_id, r.occurred_at)
            )
    return out


def _event(r: Any) -> TimelineEvent:
    return TimelineEvent(
        r.id,
        r.entity_type,
        r.entity_id,
        r.event_type,
        r.actor,
        r.authority,
        r.materiality,
        r.occurred_at,
        r.recorded_at,
        dict(r.payload or {}),
        r.evidence_id,
    )


async def events_for(
    uow: UnitOfWork, entity_type: str, entity_ids: Sequence[UUID], *, min_materiality: int = 0
) -> dict[UUID, list[TimelineEvent]]:
    """Each entity's ``context_events`` in occurrence order (timelines, chain completeness §9.2)."""
    ids = list(entity_ids)
    if not ids:
        return {}
    c = context_events_table
    rows = await uow.session.execute(
        select(c)
        .where(
            c.c.user_id == uow.user_id,
            c.c.entity_type == entity_type,
            c.c.entity_id.in_(ids),
            c.c.materiality >= min_materiality,
        )
        .order_by(c.c.entity_id, c.c.occurred_at, c.c.recorded_at, c.c.id)
    )
    out: dict[UUID, list[TimelineEvent]] = defaultdict(list)
    for r in rows:
        out[r.entity_id].append(_event(r))
    return out


async def events_recorded_between(
    uow: UnitOfWork,
    start: datetime.datetime,
    end: datetime.datetime,
    *,
    min_materiality: int = 0,
    entity_type: str | None = None,
    limit: int = 2000,
) -> list[TimelineEvent]:
    """The change feed by ``recorded_at`` (what is new to the user, CONTEXT_ARCHITECTURE.md §7.2)."""
    c = context_events_table
    stmt = select(c).where(
        c.c.user_id == uow.user_id,
        c.c.recorded_at > start,
        c.c.recorded_at <= end,
        c.c.materiality >= min_materiality,
    )
    if entity_type is not None:
        stmt = stmt.where(c.c.entity_type == entity_type)
    rows = await uow.session.execute(stmt.order_by(c.c.recorded_at, c.c.id).limit(limit))
    return [_event(r) for r in rows]


async def events_occurred_between(
    uow: UnitOfWork,
    start: datetime.datetime,
    end: datetime.datetime,
    *,
    min_materiality: int = 0,
    limit: int = 2000,
) -> list[TimelineEvent]:
    """What happened in a window by ``occurred_at``, plus events learned in it (day view §10.10)."""
    c = context_events_table
    rows = await uow.session.execute(
        select(c)
        .where(
            c.c.user_id == uow.user_id,
            c.c.materiality >= min_materiality,
            or_(
                (c.c.occurred_at >= start) & (c.c.occurred_at < end),
                (c.c.recorded_at >= start) & (c.c.recorded_at < end),
            ),
        )
        .order_by(c.c.occurred_at, c.c.id)
        .limit(limit)
    )
    return [_event(r) for r in rows]
