"""Work-item and decision read models for the API (slice 1.7, BACKEND_DESIGN.md §16.4-16.5).

Keyset pagination: each list returns the page and the sort key of its last row (the API signs
it into a cursor). Sorts use the partial indexes of migration 0008 where they apply. Evidence
sources for a page come from one query, not one per item.
"""

from __future__ import annotations

import datetime
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Generic, TypeVar
from uuid import UUID

from sqlalchemy import ColumnElement, and_, func, or_, select, tuple_

from eca.platform.errors import NotFound, ValidationFailed
from eca.platform.uow import UnitOfWork
from eca.work.models import (
    context_events_table,
    decisions_table,
    evidence_table,
    item_evidence_table,
    work_items_table,
)
from eca.work.service import WorkItemView, _view

SORTS = ("due", "priority", "created")
OPEN_STATES = ("open", "in_progress")


T = TypeVar("T")


@dataclass(frozen=True)
class Page(Generic[T]):
    items: list[T]
    next_key: list[Any] | None


@dataclass(frozen=True)
class EvidenceView:
    id: UUID
    source_item_id: UUID
    quote: str
    relation: str | None
    extraction_id: UUID | None
    occurred_at: datetime.datetime | None


@dataclass(frozen=True)
class WorkItemDetail:
    item: WorkItemView
    priority_score: float | None
    priority_reasons: list[dict[str, Any]]
    has_source_gap: bool
    conflicts: list[dict[str, Any]]
    evidence: list[EvidenceView]
    timeline: list[dict[str, Any]]


async def _sources_by_item(uow: UnitOfWork, item_ids: list[UUID]) -> dict[UUID, tuple[UUID, ...]]:
    if not item_ids:
        return {}
    ie, ev = item_evidence_table, evidence_table
    rows = await uow.session.execute(
        select(ie.c.item_id, ev.c.source_item_id)
        .join(ev, ev.c.id == ie.c.evidence_id)
        .where(ie.c.item_type == "work_item", ie.c.item_id.in_(item_ids), ie.c.relation != "superseded")
        .distinct()
    )
    out: dict[UUID, set[UUID]] = defaultdict(set)
    for r in rows:
        out[r.item_id].add(r.source_item_id)
    return {k: tuple(sorted(v)) for k, v in out.items()}


def _iso(value: datetime.datetime | None) -> str | None:
    return value.isoformat() if value else None


def _keyset(sort: str, after: list[Any] | None) -> tuple[list[Any], ColumnElement[bool] | None]:
    t = work_items_table
    if sort == "due":
        order: list[Any] = [t.c.due_at.is_(None), t.c.due_at, t.c.id]
        if after is None:
            return order, None
        is_null, due, last_id = bool(after[0]), after[1], UUID(after[2])
        if is_null:
            return order, and_(t.c.due_at.is_(None), t.c.id > last_id)
        due_at = datetime.datetime.fromisoformat(due)
        return order, or_(
            t.c.due_at.is_(None),
            and_(t.c.due_at.is_not(None), tuple_(t.c.due_at, t.c.id) > tuple_(due_at, last_id)),
        )
    if sort == "priority":
        score = func.coalesce(t.c.priority_score, -1.0)
        order = [score.desc(), t.c.id]
        if after is None:
            return order, None
        s0, last_id = float(after[0]), UUID(after[1])
        return order, or_(score < s0, and_(score == s0, t.c.id > last_id))
    order = [t.c.created_at.desc(), t.c.id.desc()]
    if after is None:
        return order, None
    created, last_id = datetime.datetime.fromisoformat(after[0]), UUID(after[1])
    return order, tuple_(t.c.created_at, t.c.id) < tuple_(created, last_id)


def _key_of(sort: str, row: Any) -> list[Any]:
    if sort == "due":
        return [row.due_at is None, _iso(row.due_at), str(row.id)]
    if sort == "priority":
        return [row.priority_score if row.priority_score is not None else -1.0, str(row.id)]
    return [_iso(row.created_at), str(row.id)]


async def list_items_page(
    uow: UnitOfWork,
    *,
    direction: str | None = None,
    status: str = "open",
    verification: str | None = None,
    person_id: UUID | None = None,
    due_before: datetime.datetime | None = None,
    sort: str = "due",
    after: list[Any] | None = None,
    limit: int = 25,
) -> Page[WorkItemView]:
    if sort not in SORTS:
        raise ValidationFailed(f"sort must be one of {SORTS}")
    t = work_items_table
    stmt = select(t).where(t.c.merged_into_id.is_(None), t.c.deleted_at.is_(None))
    if status == "open":
        stmt = stmt.where(t.c.lifecycle_status.in_(OPEN_STATES), ~t.c.archived)
    elif status == "closed":
        stmt = stmt.where(t.c.lifecycle_status.in_(("done", "cancelled")))
    elif status != "all":
        raise ValidationFailed("status must be open, closed or all")
    if verification is None:
        stmt = stmt.where(t.c.verification_status != "rejected")
    else:
        stmt = stmt.where(t.c.verification_status == verification)
    if direction is not None:
        stmt = stmt.where(t.c.direction == direction)
    if person_id is not None:
        stmt = stmt.where(
            or_(
                t.c.owner_person_id == person_id,
                t.c.counterparty_person_id == person_id,
                t.c.requester_person_id == person_id,
            )
        )
    if due_before is not None:
        stmt = stmt.where(t.c.due_at < due_before)
    order, cond = _keyset(sort, after)
    if cond is not None:
        stmt = stmt.where(cond)
    rows = (await uow.session.execute(stmt.order_by(*order).limit(limit + 1))).all()
    page, more = rows[:limit], len(rows) > limit
    sources = await _sources_by_item(uow, [r.id for r in page])
    return Page(
        [_view(r, sources.get(r.id, ())) for r in page],
        _key_of(sort, page[-1]) if more and page else None,
    )


async def evidence_of(uow: UnitOfWork, item_type: str, item_id: UUID) -> list[EvidenceView]:
    ie, ev = item_evidence_table, evidence_table
    rows = await uow.session.execute(
        select(ev.c.id, ev.c.source_item_id, ev.c.quote, ie.c.relation, ev.c.extraction_id, ev.c.occurred_at)
        .join(ie, ie.c.evidence_id == ev.c.id)
        .where(ie.c.item_type == item_type, ie.c.item_id == item_id)
        .order_by(ev.c.occurred_at, ev.c.id)
    )
    return [
        EvidenceView(r.id, r.source_item_id, r.quote, r.relation, r.extraction_id, r.occurred_at)
        for r in rows
    ]


async def get_evidence(uow: UnitOfWork, evidence_id: UUID) -> EvidenceView:
    ev = evidence_table
    row = (
        await uow.session.execute(
            select(ev.c.id, ev.c.source_item_id, ev.c.quote, ev.c.extraction_id, ev.c.occurred_at).where(
                ev.c.id == evidence_id
            )
        )
    ).one_or_none()
    if row is None:
        raise NotFound("evidence not found")
    return EvidenceView(row.id, row.source_item_id, row.quote, None, row.extraction_id, row.occurred_at)


async def item_detail(uow: UnitOfWork, item_id: UUID) -> WorkItemDetail:
    t = work_items_table
    row = (
        await uow.session.execute(select(t).where(t.c.id == item_id, t.c.deleted_at.is_(None)))
    ).one_or_none()
    if row is None:
        raise NotFound(f"work item {item_id} not found")
    sources = await _sources_by_item(uow, [row.id])
    c = context_events_table
    events = await uow.session.execute(
        select(
            c.c.id,
            c.c.event_type,
            c.c.actor,
            c.c.authority,
            c.c.materiality,
            c.c.occurred_at,
            c.c.recorded_at,
            c.c.payload,
            c.c.evidence_id,
        )
        .where(c.c.entity_type == "work_item", c.c.entity_id == item_id)
        .order_by(c.c.occurred_at, c.c.recorded_at, c.c.id)
    )
    return WorkItemDetail(
        item=_view(row, sources.get(row.id, ())),
        priority_score=row.priority_score,
        priority_reasons=list(row.priority_reasons or []),
        has_source_gap=row.has_source_gap,
        conflicts=list((row.state or {}).get("conflicts", [])),
        evidence=await evidence_of(uow, "work_item", item_id),
        timeline=[dict(e._mapping) for e in events],
    )


# --- decisions ------------------------------------------------------------------------------


@dataclass(frozen=True)
class DecisionView:
    id: UUID
    kind: str
    statement: str
    rationale: str | None
    decided_at: datetime.datetime | None
    conversation_id: UUID | None
    project_hint: str | None
    superseded_by_id: UUID | None
    resolved_by_id: UUID | None
    origin: str
    verification_status: str
    notes: str | None
    confidence: float | None
    confidence_band: str | None
    extraction_id: UUID | None
    extraction_method: str
    model: str | None
    derived_at: datetime.datetime
    user_fields: tuple[str, ...]
    version: int
    created_at: datetime.datetime | None


def decision_view(row: Any) -> DecisionView:
    return DecisionView(
        id=row.id,
        kind=row.kind,
        statement=row.statement,
        rationale=row.rationale,
        decided_at=row.decided_at,
        conversation_id=row.conversation_id,
        project_hint=row.project_hint,
        superseded_by_id=row.superseded_by_id,
        resolved_by_id=row.resolved_by_id,
        origin=row.origin,
        verification_status=row.verification_status,
        notes=row.notes,
        confidence=row.confidence,
        confidence_band=row.confidence_band,
        extraction_id=row.extraction_id,
        extraction_method=row.extraction_method,
        model=row.model,
        derived_at=row.derived_at,
        user_fields=tuple(row.user_fields or ()),
        version=row.version,
        created_at=row.created_at,
    )


async def list_decisions_page(
    uow: UnitOfWork,
    *,
    kind: str | None = None,
    since: datetime.datetime | None = None,
    after: list[Any] | None = None,
    limit: int = 25,
) -> Page[DecisionView]:
    d = decisions_table
    stmt = select(d).where(
        d.c.merged_into_id.is_(None), d.c.deleted_at.is_(None), d.c.verification_status != "rejected"
    )
    if kind is not None:
        stmt = stmt.where(d.c.kind == kind)
    if since is not None:
        stmt = stmt.where(d.c.created_at >= since)
    if after is not None:
        stmt = stmt.where(
            tuple_(d.c.created_at, d.c.id) < tuple_(datetime.datetime.fromisoformat(after[0]), UUID(after[1]))
        )
    rows = (
        await uow.session.execute(stmt.order_by(d.c.created_at.desc(), d.c.id.desc()).limit(limit + 1))
    ).all()
    page = rows[:limit]
    next_key = [_iso(page[-1].created_at), str(page[-1].id)] if len(rows) > limit and page else None
    return Page([decision_view(r) for r in page], next_key)


async def get_decision(uow: UnitOfWork, decision_id: UUID) -> DecisionView:
    d = decisions_table
    row = (
        await uow.session.execute(select(d).where(d.c.id == decision_id, d.c.deleted_at.is_(None)))
    ).one_or_none()
    if row is None:
        raise NotFound(f"decision {decision_id} not found")
    return decision_view(row)
