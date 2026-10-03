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

from sqlalchemy import Select, Text, func, literal, literal_column, or_, select
from sqlalchemy.dialects.postgresql import ARRAY

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
            ev.c.start_ms,
            ev.c.end_ms,
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
                EvidenceView(
                    r.id,
                    r.source_item_id,
                    r.quote,
                    r.relation,
                    r.extraction_id,
                    r.occurred_at,
                    r.start_ms,
                    r.end_ms,
                )
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


# --- projects (slice 2.5): hint matching with pg_trgm ------------------------------------------

HINT_MIN_SIMILARITY = 0.45


def _hint_match(column: Any, names: Sequence[str]) -> Any:
    """``column`` is trigram-similar (≥ 0.45) to any of ``names`` (lower-cased project name/aliases)."""
    n = func.unnest(literal(list(names), ARRAY(Text))).table_valued("n").alias("names")
    return (
        select(literal_column("1"))
        .select_from(n)
        .where(func.similarity(func.lower(column), n.c.n) >= HINT_MIN_SIMILARITY)
        .exists()
    )


async def items_for_project(
    uow: UnitOfWork,
    *,
    project_id: UUID,
    names: Sequence[str],
    include_closed: bool = False,
    limit: int = 50,
) -> list[WorkItemView]:
    """Items assigned to the project, or whose ``project_hint`` matches its name or aliases."""
    t = work_items_table
    lowered = [n.lower() for n in names if n]
    cond = t.c.project_id == project_id
    if lowered:
        cond = or_(
            cond,
            (t.c.project_id.is_(None))
            & t.c.project_hint.is_not(None)
            & _hint_match(t.c.project_hint, lowered),
        )
    stmt = _live_items(uow).where(cond, ~t.c.archived)
    if not include_closed:
        stmt = stmt.where(t.c.lifecycle_status.in_(OPEN_STATES))
    rows = (
        await uow.session.execute(
            stmt.order_by(t.c.last_activity_at.desc().nulls_last(), t.c.id).limit(limit)
        )
    ).all()
    sources = await _sources_by_item(uow, [r.id for r in rows])
    return [_view(r, sources.get(r.id, ())) for r in rows]


async def decisions_for_hints(
    uow: UnitOfWork, names: Sequence[str], *, limit: int = 20
) -> list[DecisionView]:
    lowered = [n.lower() for n in names if n]
    if not lowered:
        return []
    d = decisions_table
    rows = (
        await uow.session.execute(
            _live_decisions(uow)
            .where(d.c.project_hint.is_not(None), _hint_match(d.c.project_hint, lowered))
            .order_by(d.c.created_at.desc(), d.c.id)
            .limit(limit)
        )
    ).all()
    return [decision_view(r) for r in rows]


@dataclass(frozen=True)
class HintSource:
    hint: str
    source_item_id: UUID
    person_ids: tuple[UUID, ...]


async def project_hint_sources(uow: UnitOfWork) -> list[HintSource]:
    """(hint, source) pairs of live items and decisions, for project suggestions (§12.5)."""
    t, d, ie, ev = work_items_table, decisions_table, item_evidence_table, evidence_table
    out: list[HintSource] = []
    item_rows = await uow.session.execute(
        select(t.c.project_hint, ev.c.source_item_id, t.c.owner_person_id, t.c.counterparty_person_id)
        .join(ie, (ie.c.item_type == "work_item") & (ie.c.item_id == t.c.id))
        .join(ev, ev.c.id == ie.c.evidence_id)
        .where(
            t.c.user_id == uow.user_id,
            t.c.project_hint.is_not(None),
            t.c.verification_status != "rejected",
            t.c.merged_into_id.is_(None),
            t.c.deleted_at.is_(None),
        )
    )
    for r in item_rows:
        persons = tuple(p for p in (r.owner_person_id, r.counterparty_person_id) if p is not None)
        out.append(HintSource(r.project_hint, r.source_item_id, persons))
    decision_rows = await uow.session.execute(
        select(d.c.project_hint, ev.c.source_item_id)
        .join(ie, (ie.c.item_type == "decision") & (ie.c.item_id == d.c.id))
        .join(ev, ev.c.id == ie.c.evidence_id)
        .where(
            d.c.user_id == uow.user_id,
            d.c.project_hint.is_not(None),
            d.c.verification_status != "rejected",
            d.c.deleted_at.is_(None),
        )
    )
    for dr in decision_rows:
        out.append(HintSource(dr.project_hint, dr.source_item_id, ()))
    return out


@dataclass(frozen=True)
class PersonWork:
    """Inputs of the relationship profile (CONTEXT_ARCHITECTURE.md §5.3) for one person."""

    open_mine: int  # open my_commitment / my_task involving the person (the user owes them)
    open_theirs: int  # open waiting_for / delegated involving the person (they owe the user)
    hints: tuple[tuple[str, datetime.datetime], ...]  # AI-derived project hints with their activity time


MINE = ("my_commitment", "my_task")
THEIRS = ("waiting_for", "delegated")


async def person_work(
    uow: UnitOfWork, person_ids: Sequence[UUID], *, since: datetime.datetime
) -> dict[UUID, PersonWork]:
    """Open-item counts both directions and recent project hints per person (live, not rejected)."""
    ids = sorted(set(person_ids))
    if not ids:
        return {}
    t = work_items_table
    rows = (
        await uow.session.execute(
            _live_items(uow)
            .with_only_columns(
                t.c.owner_person_id,
                t.c.counterparty_person_id,
                t.c.requester_person_id,
                t.c.direction,
                t.c.lifecycle_status,
                t.c.archived,
                t.c.project_hint,
                t.c.last_activity_at,
                t.c.created_at,
            )
            .where(
                or_(
                    t.c.owner_person_id.in_(ids),
                    t.c.counterparty_person_id.in_(ids),
                    t.c.requester_person_id.in_(ids),
                ),
                or_(t.c.lifecycle_status.in_(OPEN_STATES), t.c.last_activity_at >= since),
            )
        )
    ).all()
    mine: dict[UUID, int] = defaultdict(int)
    theirs: dict[UUID, int] = defaultdict(int)
    hints: dict[UUID, list[tuple[str, datetime.datetime]]] = defaultdict(list)
    wanted = set(ids)
    for r in rows:
        involved = {r.owner_person_id, r.counterparty_person_id, r.requester_person_id} & wanted
        is_open = r.lifecycle_status in OPEN_STATES and not r.archived
        at = r.last_activity_at or r.created_at
        for pid in involved:
            if is_open and r.direction in MINE:
                mine[pid] += 1
            elif is_open and r.direction in THEIRS:
                theirs[pid] += 1
            if r.project_hint and at is not None and at >= since:
                hints[pid].append((r.project_hint, at))
    return {pid: PersonWork(mine[pid], theirs[pid], tuple(hints[pid])) for pid in ids}


REMINDER_ITEMS_MAX = 1000


async def open_items(uow: UnitOfWork, item_ids: Sequence[UUID] | None = None) -> list[WorkItemView]:
    """Open, live, non-rejected items (reminder rules): all of the user's, or the given ones."""
    t = work_items_table
    stmt = _live_items(uow).where(t.c.lifecycle_status.in_(OPEN_STATES), ~t.c.archived)
    if item_ids is not None:
        if not item_ids:
            return []
        stmt = stmt.where(t.c.id.in_(list(item_ids)))
    rows = await uow.session.execute(stmt.order_by(t.c.due_at.nulls_last(), t.c.id).limit(REMINDER_ITEMS_MAX))
    return [_view(r) for r in rows]


async def user_activity_since(
    uow: UnitOfWork, entity_type: str, since: dict[UUID, datetime.datetime]
) -> set[UUID]:
    """Entities with a user event recorded after their own ``since`` (acted-upon detection, §15.2)."""
    if not since:
        return set()
    c = context_events_table
    rows = await uow.session.execute(
        select(c.c.entity_id, func.max(c.c.recorded_at).label("at"))
        .where(
            c.c.user_id == uow.user_id,
            c.c.entity_type == entity_type,
            c.c.entity_id.in_(list(since)),
            c.c.actor == "user",
        )
        .group_by(c.c.entity_id)
    )
    return {r.entity_id for r in rows if r.at > since[r.entity_id]}


async def source_item_stats(uow: UnitOfWork, source_item_ids: Sequence[UUID]) -> tuple[set[UUID], set[UUID]]:
    """(sources with at least one live, non-rejected work item; those with an item that has a
    deadline) — the daily email summary counts (PRD §12)."""
    ids = list(source_item_ids)
    if not ids:
        return set(), set()
    ie, ev, t = item_evidence_table, evidence_table, work_items_table
    rows = await uow.session.execute(
        select(ev.c.source_item_id, t.c.due_at)
        .join(ie, ie.c.evidence_id == ev.c.id)
        .join(t, (t.c.id == ie.c.item_id) & (ie.c.item_type == "work_item"))
        .where(
            ev.c.user_id == uow.user_id,
            ev.c.source_item_id.in_(ids),
            t.c.verification_status != "rejected",
            t.c.merged_into_id.is_(None),
            t.c.deleted_at.is_(None),
        )
    )
    with_items: set[UUID] = set()
    with_deadlines: set[UUID] = set()
    for r in rows:
        with_items.add(r.source_item_id)
        if r.due_at is not None:
            with_deadlines.add(r.source_item_id)
    return with_items, with_deadlines
