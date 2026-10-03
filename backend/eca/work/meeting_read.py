"""Meeting read views owned by ``work`` (slice 4.3). Deterministic SQL; no model call.

- :func:`meeting_work`: the items, decisions and open questions that came from a meeting's
  recording (evidence with transcript offsets).
- :func:`changes_between`: "what changed since the previous meeting" (TECHNICAL_DESIGN.md §16.1):
  ``context_events`` recorded after the previous related meeting ended and up to this meeting's
  end, on items and decisions that involve the meeting's participants or came from either
  meeting's sources, materiality ≥ 2, folded into net changes per entity (CONTEXT_ARCHITECTURE.md
  §7.4: a deadline moved and moved back is no net change).
- :func:`open_questions_for`: unresolved open questions (not resolved, superseded or rejected)
  from given meetings or involving given persons (prep and the ``meeting_prep`` reminder).
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select

from eca.platform.uow import UnitOfWork
from eca.work.fold import FoldEvent, fold
from eca.work.models import decisions_table, evidence_table
from eca.work.queries import DecisionView, EvidenceView, decision_view
from eca.work.read import (
    TimelineEvent,
    _live_decisions,
    events_for,
    events_recorded_between,
    evidence_for,
    item_ids_for_sources,
    items_by_ids,
)
from eca.work.service import WorkItemView

MIN_MATERIALITY = 2
TRACKED = (
    "lifecycle_status",
    "verification_status",
    "reported_status",
    "due_at",
    "owner_person_id",
    "counterparty_person_id",
    "direction",
    "title",
)


@dataclass(frozen=True)
class MeetingWork:
    items: list[WorkItemView]
    decisions: list[DecisionView]
    open_questions: list[DecisionView]
    evidence: dict[UUID, list[EvidenceView]]  # item or decision → transcript evidence


async def meeting_work(uow: UnitOfWork, *, meeting_id: UUID, source_item_id: UUID | None) -> MeetingWork:
    items: list[WorkItemView] = []
    if source_item_id is not None:
        by_item, _ = await item_ids_for_sources(uow, [source_item_id])
        items = await items_by_ids(uow, sorted(by_item))
    d = decisions_table
    rows = (
        await uow.session.execute(
            _live_decisions(uow).where(d.c.meeting_id == meeting_id).order_by(d.c.created_at, d.c.id)
        )
    ).all()
    views = [decision_view(r) for r in rows]
    evidence = await evidence_for(uow, "work_item", [i.id for i in items], per_item=3)
    evidence.update(await evidence_for(uow, "decision", [v.id for v in views], per_item=2))
    return MeetingWork(
        items=items,
        decisions=[v for v in views if v.kind == "decision"],
        open_questions=[v for v in views if v.kind == "open_question"],
        evidence=evidence,
    )


async def open_questions_for(
    uow: UnitOfWork,
    *,
    meeting_ids: Sequence[UUID] = (),
    conversation_ids: Sequence[UUID] = (),
    limit: int = 20,
) -> list[DecisionView]:
    """Open questions not resolved, superseded or rejected, from these meetings or threads."""
    if not meeting_ids and not conversation_ids:
        return []
    d = decisions_table
    conditions = []
    if meeting_ids:
        conditions.append(d.c.meeting_id.in_(list(meeting_ids)))
    if conversation_ids:
        conditions.append(d.c.conversation_id.in_(list(conversation_ids)))
    rows = (
        await uow.session.execute(
            _live_decisions(uow)
            .where(
                d.c.kind == "open_question",
                d.c.resolved_by_id.is_(None),
                d.c.superseded_by_id.is_(None),
                or_(*conditions),
            )
            .order_by(d.c.created_at.desc(), d.c.id)
            .limit(limit)
        )
    ).all()
    return [decision_view(r) for r in rows]


# ---------------------------------------------------------------- what changed


@dataclass(frozen=True)
class MeetingChange:
    entity_type: str  # work_item | decision
    entity_id: UUID
    kind: str  # new | status | deadline | owner | conflict | decided | resolved | superseded
    materiality: int
    recorded_at: datetime.datetime
    before: dict[str, Any] = field(default_factory=dict)
    after: dict[str, Any] = field(default_factory=dict)
    events: int = 0


def _fold_events(events: Sequence[TimelineEvent]) -> list[FoldEvent]:
    return [
        FoldEvent(
            str(e.id),
            e.event_type,
            e.actor,
            e.authority,
            e.occurred_at,
            e.recorded_at,
            e.payload,
            str(e.evidence_id) if e.evidence_id else None,
        )
        for e in events
    ]


def _tracked(fields: dict[str, Any]) -> dict[str, Any]:
    return {
        name: (v.isoformat() if isinstance(v := fields.get(name), datetime.datetime) else v)
        for name in TRACKED
    }


def item_change(
    events: Sequence[TimelineEvent], *, since: datetime.datetime, until: datetime.datetime
) -> MeetingChange | None:
    """Net change of one item between ``since`` and ``until`` (by ``recorded_at``), or None."""
    window = [e for e in events if since < e.recorded_at <= until]
    material = [e for e in window if e.materiality >= MIN_MATERIALITY]
    if not material:
        return None
    history = [e for e in events if e.recorded_at <= until]
    before_events = [e for e in events if e.recorded_at <= since]
    after = _tracked(fold(_fold_events(history)).fields)
    latest = max(e.recorded_at for e in window)
    level = max(e.materiality for e in material)
    entity_id = events[0].entity_id
    if not before_events:
        return MeetingChange("work_item", entity_id, "new", level, latest, {}, after, len(window))
    before = _tracked(fold(_fold_events(before_events)).fields)
    changed = sorted(k for k in TRACKED if before.get(k) != after.get(k))
    if any(e.event_type == "conflict_detected" for e in window):
        kind = "conflict"
    elif "due_at" in changed:
        kind = "deadline"
    elif {"lifecycle_status", "verification_status", "reported_status"} & set(changed):
        kind = "status"
    elif {"owner_person_id", "counterparty_person_id", "direction"} & set(changed):
        kind = "owner"
    else:
        return None
    return MeetingChange(
        "work_item",
        entity_id,
        kind,
        level,
        latest,
        {k: before.get(k) for k in changed},
        {k: after.get(k) for k in changed},
        len(window),
    )


_DECISION_KIND = {
    "created": "decided",
    "resolved": "resolved",
    "superseded": "superseded",
    "conflict_detected": "conflict",
}


async def changes_between(
    uow: UnitOfWork,
    *,
    since: datetime.datetime,
    until: datetime.datetime,
    person_ids: set[UUID],
    source_item_ids: Sequence[UUID],
    meeting_ids: Sequence[UUID],
) -> list[MeetingChange]:
    """Net changes between two meetings, most material first. No AI call."""
    if until <= since:
        return []
    events = await events_recorded_between(uow, since, until, min_materiality=MIN_MATERIALITY)
    item_ids = sorted({e.entity_id for e in events if e.entity_type == "work_item"})
    decision_ids = sorted({e.entity_id for e in events if e.entity_type == "decision"})
    from_sources, decisions_from_sources = await item_ids_for_sources(uow, list(source_item_ids))
    views = {v.id: v for v in await items_by_ids(uow, item_ids)}
    histories = await events_for(uow, "work_item", item_ids)
    changes: list[MeetingChange] = []
    for item_id in item_ids:
        view = views.get(item_id)
        if view is None:
            continue
        involved = {view.owner_person_id, view.counterparty_person_id, view.requester_person_id} & person_ids
        if not involved and item_id not in from_sources:
            continue
        change = item_change(histories.get(item_id, []), since=since, until=until)
        if change is not None:
            changes.append(change)
    d = decisions_table
    in_meetings = set()
    if decision_ids:
        rows = await uow.session.execute(
            select(d.c.id, d.c.meeting_id).where(d.c.user_id == uow.user_id, d.c.id.in_(decision_ids))
        )
        in_meetings = {r.id for r in rows if r.meeting_id in set(meeting_ids)}
    for decision_id in decision_ids:
        if decision_id not in in_meetings and decision_id not in decisions_from_sources:
            continue
        mine = [e for e in events if e.entity_type == "decision" and e.entity_id == decision_id]
        last = mine[-1]
        changes.append(
            MeetingChange(
                "decision",
                decision_id,
                _DECISION_KIND.get(last.event_type, last.event_type),
                max(e.materiality for e in mine),
                last.recorded_at,
                events=len(mine),
            )
        )
    changes.sort(key=lambda c: (-c.materiality, -c.recorded_at.timestamp(), str(c.entity_id)))
    return changes


async def transcript_evidence(
    uow: UnitOfWork, evidence_ids: Sequence[UUID]
) -> dict[UUID, tuple[int | None, int | None]]:
    """Offsets of transcript evidence rows (citations that point at a moment of a meeting)."""
    ids = list(evidence_ids)
    if not ids:
        return {}
    ev = evidence_table
    rows = await uow.session.execute(
        select(ev.c.id, ev.c.start_ms, ev.c.end_ms).where(ev.c.user_id == uow.user_id, ev.c.id.in_(ids))
    )
    return {r.id: (r.start_ms, r.end_ms) for r in rows}
