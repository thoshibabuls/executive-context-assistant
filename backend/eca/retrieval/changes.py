"""Change feed with net-change folding (CONTEXT_ARCHITECTURE.md §7.3-§7.4, §10.11) and the day
view (§10.10). Deterministic: no model call.

What changed since an anchor = ``context_events`` recorded after it (materiality ≥ 2), folded per
entity: the item's state folded at the anchor is compared with its state now, so a deadline moved
and moved back is no net change. Decisions and new awaiting replies have no context events yet,
so they come from their own tables by the time they were recorded.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from eca import communication, meetings, work
from eca.platform.uow import UnitOfWork
from eca.work import FoldEvent, TimelineEvent, WorkItemView

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
TIME_EVENTS = {"became_overdue": "overdue", "due_soon": "due_soon", "became_stale": "stale"}
GROUP_ORDER = (
    "new_mine",
    "new_theirs",
    "new_other",
    "status_changes",
    "deadline_changes",
    "conflicts",
    "decisions",
    "awaiting_replies",
    "overdue_or_stale",
    "meetings",
)


@dataclass(frozen=True)
class NetChange:
    group: str
    entity_type: str  # work_item | decision | conversation | meeting
    entity_id: UUID
    materiality: int
    rank: float
    recorded_at: datetime.datetime
    before: dict[str, Any] = field(default_factory=dict)
    after: dict[str, Any] = field(default_factory=dict)
    detail: tuple[str, ...] = ()  # e.g. "deadline discussed twice" (no net change)
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
    out = {}
    for name in TRACKED:
        value = fields.get(name)
        out[name] = value.isoformat() if isinstance(value, datetime.datetime) else value
    return out


def item_net_change(
    events: Sequence[TimelineEvent], *, anchor: datetime.datetime, direction: str, priority: float | None
) -> NetChange | None:
    """Net change of one work item since ``anchor`` (by ``recorded_at``), or None."""
    new = [e for e in events if e.recorded_at > anchor]
    if not new:
        return None
    material = [e for e in new if e.materiality >= MIN_MATERIALITY]
    if not material:
        return None
    before_events = [e for e in events if e.recorded_at <= anchor]
    after = _tracked(work.fold(_fold_events(events)).fields)
    materiality = max(e.materiality for e in material)
    rank = materiality * (1 + (priority or 50.0) / 100.0)
    latest = max(e.recorded_at for e in new)
    entity_id = events[0].entity_id
    if not before_events:
        group = {
            "my_commitment": "new_mine",
            "my_task": "new_mine",
            "waiting_for": "new_theirs",
            "delegated": "new_theirs",
        }.get(direction, "new_other")
        return NetChange(group, "work_item", entity_id, materiality, rank, latest, {}, after, (), len(new))
    before = _tracked(work.fold(_fold_events(before_events)).fields)
    changed = {k for k in TRACKED if before.get(k) != after.get(k)}
    detail: list[str] = []
    due_events = [e for e in new if e.event_type == "due_changed"]
    if due_events and "due_at" not in changed:
        detail.append(f"the deadline was discussed {len(due_events)} time(s) with no net change")
    if any(e.event_type == "conflict_detected" for e in new):
        group = "conflicts"
    elif "due_at" in changed:
        group = "deadline_changes"
    elif changed & {
        "lifecycle_status",
        "verification_status",
        "reported_status",
        "owner_person_id",
        "direction",
    }:
        group = "status_changes"
    elif any(e.event_type in TIME_EVENTS for e in material):
        group = "overdue_or_stale"
        detail.extend(sorted({TIME_EVENTS[e.event_type] for e in material if e.event_type in TIME_EVENTS}))
    else:
        return None  # nothing net changed (§7.4): dropped unless the user asks for detail
    return NetChange(
        group,
        "work_item",
        entity_id,
        materiality,
        rank,
        latest,
        {k: before.get(k) for k in sorted(changed)},
        {k: after.get(k) for k in sorted(changed)},
        tuple(detail),
        len(new),
    )


@dataclass(frozen=True)
class ChangeSet:
    changes: list[NetChange]
    items: dict[UUID, WorkItemView]
    decisions: dict[UUID, work.DecisionView]
    conversations: dict[UUID, communication.ConversationSummary]
    meetings: dict[UUID, meetings.MeetingDetail]


async def changes_since(
    uow: UnitOfWork,
    *,
    anchor: datetime.datetime,
    now: datetime.datetime,
    person_ids: Sequence[UUID] = (),
    item_filter: set[UUID] | None = None,
) -> ChangeSet:
    """All net changes since ``anchor``, ranked by materiality * priority (§10.11 steps 2-5)."""
    events = await work.events_recorded_between(
        uow, anchor, now, min_materiality=MIN_MATERIALITY, entity_type="work_item"
    )
    touched = sorted({e.entity_id for e in events})
    histories = await work.events_for(uow, "work_item", touched)
    views = {v.id: v for v in await work.items_by_ids(uow, touched)}
    people = set(person_ids)
    changes: list[NetChange] = []
    for item_id in touched:
        view = views.get(item_id)
        if view is None or (item_filter is not None and item_id not in item_filter):
            continue
        if (
            people
            and not {view.owner_person_id, view.counterparty_person_id, view.requester_person_id} & people
        ):
            continue
        change = item_net_change(
            histories.get(item_id, []), anchor=anchor, direction=view.direction, priority=view.priority_score
        )
        if change is not None:
            changes.append(change)
    decisions = {} if people else {d.id: d for d in await work.decisions_created_between(uow, anchor, now)}
    for d in decisions.values():
        materiality = 3 if d.kind == "decision" else 2
        changes.append(
            NetChange(
                "decisions", "decision", d.id, materiality, float(materiality), d.created_at or now, events=1
            )
        )
    convs = await communication.awaiting_user_since(uow, anchor)
    if people:
        latest = await communication.latest_messages(uow, [c.id for c in convs])
        convs = [c for c in convs if (msg := latest.get(c.id)) is not None and msg.sender_person_id in people]
    for c in convs:
        rank = 2 * (1 + (c.priority_score or 50.0) / 100.0)
        changes.append(
            NetChange("awaiting_replies", "conversation", c.id, 2, rank, c.last_inbound_at or now, events=1)
        )
    held = await meetings.meeting_details_between(uow, anchor, now, person_ids=list(person_ids) or None)
    held = [mt for mt in held if mt.ends_at <= now]
    for mt in held:
        changes.append(NetChange("meetings", "meeting", mt.id, 2, 2.0, mt.ends_at))
    changes.sort(key=lambda c: (-c.rank, str(c.entity_id)))  # also the keyset order of /changes
    return ChangeSet(changes, views, decisions, {cv.id: cv for cv in convs}, {mt.id: mt for mt in held})


@dataclass(frozen=True)
class DayView:
    start: datetime.datetime
    end: datetime.datetime
    events: list[TimelineEvent]  # materiality ≥ 2, by occurrence
    learned_today: set[UUID]  # event IDs recorded in the day but occurred earlier
    items: dict[UUID, WorkItemView]
    meetings: list[meetings.MeetingDetail]
    conversations: list[communication.ConversationSummary]
    decisions: list[work.DecisionView]
    awaiting: list[communication.ConversationSummary]


async def day_view(uow: UnitOfWork, *, start: datetime.datetime, end: datetime.datetime) -> DayView:
    """The structured day view (§10.10): what happened in [start, end), on demand (no stored digest)."""
    events = [
        e
        for e in await work.events_occurred_between(uow, start, end, min_materiality=MIN_MATERIALITY)
        if e.entity_type == "work_item"
    ]
    learned = {e.id for e in events if e.occurred_at < start and start <= e.recorded_at < end}
    item_ids = sorted({e.entity_id for e in events})
    items = {v.id: v for v in await work.items_by_ids(uow, item_ids)}
    events = [e for e in events if e.entity_id in items]
    held = [m for m in await meetings.meeting_details_between(uow, start, end) if m.ends_at <= end]
    convs = await communication.conversations_active_between(uow, start, end, limit=30)
    decisions = await work.decisions_created_between(uow, start, end)
    awaiting = [
        c
        for c in await communication.awaiting_user_since(uow, start)
        if c.last_inbound_at and c.last_inbound_at < end
    ]
    return DayView(start, end, events, learned, items, held, convs, decisions, awaiting)
