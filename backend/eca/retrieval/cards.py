"""Deterministic cards (CONTEXT_ARCHITECTURE.md §9.5, §9.10). Same inputs, same text.

Every card states its data class and authority so the model and the user can tell source facts,
AI inferences, user-authored values and computed status apart (§4.1). Untrusted text (titles,
quotes, names, subjects) is delimited by ``<<<`` and ``>>>``; the markers inside content are
neutralized, so packed content cannot close the delimiter (TECHNICAL_DESIGN.md §17.6). Dates are
written as ``Thu 8 Oct 2026`` so the grounding checks can match them (AI_PIPELINE.md §5.8). User
notes are never rendered.
"""

from __future__ import annotations

import contextlib
import datetime
from collections.abc import Mapping
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from eca.communication import ConversationSummary
from eca.meetings import MeetingDetail
from eca.people import PersonRef
from eca.retrieval.packet import PacketItem, Priority, Section
from eca.work import DecisionView, EvidenceView, TimelineEvent, WorkItemView

DIRECTION_TEXT = {
    "my_task": "your task",
    "my_commitment": "you committed",
    "delegated": "delegated by you",
    "waiting_for": "waiting for them",
    "shared": "shared",
    "observed": "observed (between others)",
    "unresolved": "owner unclear",
}
EVENT_TEXT = {
    "created": "detected",
    "restated": "restated",
    "status_signal": "status reported",
    "completed_claim": "completion claimed (not confirmed)",
    "due_changed": "deadline changed",
    "accepted": "accepted",
    "user_edit": "you edited",
    "user_confirmed": "you confirmed",
    "user_rejected": "you rejected",
    "lifecycle_changed": "you changed the status",
    "conflict_detected": "conflicting information detected",
    "became_overdue": "became overdue",
    "due_soon": "due within 24 hours",
    "became_stale": "no recent activity",
    "source_removed": "a source was deleted",
    "merged": "merged",
    "user_project": "you assigned a project",
}


NEUTRAL_OPEN = "\u2039\u2039\u2039"  # replaces "<<<" inside content (CONTEXT_ARCHITECTURE.md §9.10)
NEUTRAL_CLOSE = "\u203a\u203a\u203a"  # replaces ">>>"


def delimit(value: str | None) -> str:
    text = (value or "").replace("<<<", NEUTRAL_OPEN).replace(">>>", NEUTRAL_CLOSE)
    return f"<<<{text}>>>"


def fmt_date(moment: datetime.datetime | None, tz: ZoneInfo, *, with_time: bool = False) -> str:
    if moment is None:
        return "no date"
    local = moment.astimezone(tz)
    day = f"{local.strftime('%a')} {local.day} {local.strftime('%b %Y')}"
    if with_time:
        return f"{day} {local.strftime('%H:%M')} {local.tzname() or ''}".rstrip()
    return day


def person_label(person_id: UUID | None, persons: Mapping[UUID, PersonRef], self_id: UUID) -> str:
    if person_id is None:
        return "unknown"
    if person_id == self_id:
        return "you"
    ref = persons.get(person_id)
    if ref is None:
        return "unknown"
    return ref.display_name or ref.primary_email or "unknown"


def _item_provenance(v: WorkItemView) -> tuple[str, str, bool, int]:
    """(data class, label, user backed, authority)."""
    user_backed = (
        v.origin == "user" or v.verification_status in ("confirmed", "user_created") or bool(v.user_fields)
    )
    if v.origin == "user":
        return "user", "added by you", True, 5
    if v.verification_status == "confirmed":
        return "user", "AI-detected, confirmed by you", True, 5
    band = v.confidence_band or "unknown"
    label = f"AI suggestion, {band} confidence"
    if v.user_fields:
        label += f"; you set {', '.join(v.user_fields)}"
    authority = {"explicit": 4, "probable": 3}.get(v.commitment_strength or "", 2)
    return "ai_derived", label, user_backed, authority


def _due(v: WorkItemView, tz: ZoneInfo) -> str | None:
    if v.due_at is None:
        return f'no date ("{v.due_text}")' if v.due_text else None
    when = fmt_date(v.due_at, tz, with_time=v.due_precision == "datetime")
    return f'{when} ("{v.due_text}")' if v.due_text else when


def _counterpart(v: WorkItemView, persons: Mapping[UUID, PersonRef], self_id: UUID) -> str:
    if v.direction in ("waiting_for", "delegated", "observed", "unresolved"):
        return person_label(v.owner_person_id, persons, self_id)
    if v.direction == "my_task":
        return person_label(v.requester_person_id or v.counterparty_person_id, persons, self_id)
    return person_label(v.counterparty_person_id, persons, self_id)


def item_card(
    v: WorkItemView,
    persons: Mapping[UUID, PersonRef],
    self_id: UUID,
    tz: ZoneInfo,
    now: datetime.datetime,
    *,
    section: Section = "state",
    priority: Priority = "other_state",
    score: float = 1.0,
) -> PacketItem:
    data_class, label, user_backed, authority = _item_provenance(v)
    due = _due(v, tz)
    overdue = v.due_at is not None and v.due_at < now and v.lifecycle_status in ("open", "in_progress")
    parts = [
        f"[{v.type} · {DIRECTION_TEXT.get(v.direction, v.direction)} · {label}]",
        delimit(v.title),
        f"owner: {delimit(person_label(v.owner_person_id, persons, self_id))}",
    ]
    if v.counterparty_person_id:
        parts.append(f"counterparty: {delimit(person_label(v.counterparty_person_id, persons, self_id))}")
    if v.requester_person_id:
        parts.append(f"requested by: {delimit(person_label(v.requester_person_id, persons, self_id))}")
    parts.append(f"due: {due or 'none'}{' (overdue)' if overdue else ''}")
    parts.append(f"lifecycle (authoritative): {v.lifecycle_status}")
    if v.reported_status:
        parts.append(
            f"reported status (not confirmed): {delimit(v.reported_status)}"
            + (f" on {fmt_date(v.reported_status_at, tz)}" if v.reported_status_at else "")
        )
    if v.commitment_strength:
        parts.append(f"strength: {v.commitment_strength}")
    if v.has_conflict:
        parts.append("conflict: sources disagree (see timeline)")
    if v.stale:
        parts.append(f"stale: no activity since {fmt_date(v.last_activity_at, tz)}")
    if v.has_source_gap:
        parts.append("a supporting source was deleted")
    counterpart = _counterpart(v, persons, self_id)
    line_bits = [counterpart, v.title]
    if due:
        line_bits.append(f"due {due}" + (" (overdue)" if overdue else ""))
    if v.reported_status:
        line_bits.append(f"reported: {v.reported_status}")
    if v.has_conflict:
        line_bits.append("conflicting information")
    if data_class != "user":
        line_bits.append("possible" if v.confidence_band == "low" else "AI suggestion")
    claim = "user" if user_backed else ("source" if v.commitment_strength == "explicit" else "inference")
    return PacketItem(
        key=f"work_item:{v.id}",
        kind="work_item",
        section=section,
        priority=priority,
        text=" · ".join(parts),
        line=" — ".join(line_bits),
        data_class=data_class,
        claim_kind=claim,
        authority=authority,
        entity_id=v.id,
        source_item_ids=v.evidence_source_ids,
        as_of=v.last_activity_at or v.derived_at,
        score=score,
        user_backed=user_backed,
        group=counterpart,
    )


def decision_card(
    d: DecisionView,
    tz: ZoneInfo,
    *,
    sources: tuple[UUID, ...] = (),
    section: Section = "state",
    priority: Priority = "other_state",
    score: float = 1.0,
) -> PacketItem:
    user_backed = (
        d.origin == "user" or d.verification_status in ("confirmed", "user_created") or bool(d.user_fields)
    )
    label = (
        "confirmed by you" if user_backed else f"AI suggestion, {d.confidence_band or 'unknown'} confidence"
    )
    state = "open question" if d.kind == "open_question" else "decision"
    if d.superseded_by_id:
        state += ", superseded"
    if d.resolved_by_id:
        state += ", resolved"
    when = fmt_date(d.decided_at or d.created_at, tz)
    return PacketItem(
        key=f"decision:{d.id}",
        kind="decision",
        section=section,
        priority=priority,
        text=f"[{state} · {label}] {delimit(d.statement)} · recorded {when}",
        line=f"{d.statement} — {state}, {when}" + ("" if user_backed else " — AI suggestion"),
        data_class="user" if user_backed else "ai_derived",
        claim_kind="user" if user_backed else "inference",
        authority=5 if user_backed else 2,
        entity_id=d.id,
        source_item_ids=sources,
        as_of=d.decided_at or d.created_at,
        score=score,
        user_backed=user_backed,
    )


def conversation_card(
    c: ConversationSummary,
    tz: ZoneInfo,
    *,
    sender: str | None = None,
    sources: tuple[UUID, ...] = (),
    section: Section = "state",
    priority: Priority = "other_state",
    score: float = 1.0,
) -> PacketItem:
    state = {
        "user": "awaiting your reply",
        "other": "awaiting the other side",
        "none": "no reply pending",
    }.get(c.awaiting, c.awaiting)
    reasons = "; ".join(str(r.get("text", "")) for r in c.priority_reasons if r.get("text"))
    parts = [f"[email thread · {state} · computed]", delimit(c.subject or "(no subject)")]
    if sender:
        parts.append(f"last from {delimit(sender)}")
    parts.append(f"last inbound {fmt_date(c.last_inbound_at, tz)}")
    if c.latest_snippet:
        parts.append(f"latest: {delimit(c.latest_snippet)}")
    if reasons:
        parts.append(f"priority: {reasons}")
    since = fmt_date(c.last_inbound_at, tz)
    line = f"{sender or 'unknown sender'} — {c.subject or '(no subject)'} — {state} since {since}"
    if reasons:
        line += f" — {reasons}"
    return PacketItem(
        key=f"conversation:{c.id}",
        kind="conversation",
        section=section,
        priority=priority,
        text=" · ".join(parts),
        line=line,
        data_class="computed",
        claim_kind="source",
        authority=1,
        entity_id=c.id,
        source_item_ids=sources,
        as_of=c.last_message_at,
        score=score,
        group=sender,
    )


def meeting_card(
    m: MeetingDetail,
    persons: Mapping[UUID, PersonRef],
    self_id: UUID,
    tz: ZoneInfo,
    now: datetime.datetime,
    *,
    section: Section = "anchors",
    priority: Priority = "anchor",
    score: float = 1.0,
) -> PacketItem:
    names = [person_label(p, persons, self_id) for p in m.attendee_ids if p != self_id]
    when = f"{fmt_date(m.starts_at, tz, with_time=True)} to {m.ends_at.astimezone(tz).strftime('%H:%M')}"
    status = "cancelled" if m.status == "cancelled" else ("held" if m.ends_at <= now else "scheduled")
    return PacketItem(
        key=f"meeting:{m.id}",
        kind="meeting",
        section=section,
        priority=priority,
        text=f"[meeting · calendar · {status}] {delimit(m.title or '(untitled)')} · {when} · attendees: "
        + delimit(", ".join(names) or "none"),
        line=f"{m.title or '(untitled)'} — {when} — {', '.join(names) or 'no other attendees'}",
        data_class="source",
        claim_kind="source",
        authority=4,
        entity_id=m.id,
        source_item_ids=(m.source_item_id,),
        as_of=m.starts_at,
        score=score,
    )


def person_card(
    person: PersonRef,
    tz: ZoneInfo,
    *,
    role: str | None = None,
    organization: str | None = None,
    last_interaction_at: datetime.datetime | None = None,
    you_owe: int = 0,
    they_owe: int = 0,
    next_meeting: str | None = None,
    score: float = 1.0,
) -> PacketItem:
    name = person.display_name or person.primary_email or "unknown"
    parts = [f"[person] {delimit(name)}"]
    if person.primary_email:
        parts.append(f"email {delimit(person.primary_email)}")
    if organization:
        parts.append(f"organization {delimit(organization)}")
    if role:
        parts.append(f"role {delimit(role)} (inferred unless you set it)")
    parts.append(f"last interaction {fmt_date(last_interaction_at, tz)}")
    parts.append(f"open items: you owe {you_owe}, they owe {they_owe}")
    if next_meeting:
        parts.append(f"next meeting: {delimit(next_meeting)}")
    return PacketItem(
        key=f"person:{person.id}",
        kind="person",
        section="anchors",
        priority="anchor",
        text=" · ".join(parts),
        line=f"{name} — you owe {you_owe}, they owe {they_owe}",
        data_class="computed",
        claim_kind="source",
        authority=3,
        entity_id=person.id,
        score=score,
    )


def quote_card(
    e: EvidenceView,
    tz: ZoneInfo,
    *,
    origin: str,
    item_key: str | None = None,
    priority: Priority = "evidence",
    score: float = 1.0,
) -> PacketItem:
    """A verbatim evidence quote (SOURCE). ``origin`` names the source, e.g. "email from John"."""
    return PacketItem(
        key=f"quote:{e.id}",
        kind="quote",
        section="support",
        priority=priority,
        text=f"[quote · {delimit(origin)} · {fmt_date(e.occurred_at, tz)}"
        + (f" · for {item_key}" if item_key else "")
        + f"] {delimit(e.quote)}",
        line=f'"{e.quote}" ({origin}, {fmt_date(e.occurred_at, tz)})',
        data_class="source",
        claim_kind="source",
        authority=3,
        entity_id=e.id,
        source_item_ids=(e.source_item_id,),
        evidence_ids=(e.id,),
        as_of=e.occurred_at,
        score=score,
    )


def chunk_card(row: Any, tz: ZoneInfo, *, score: float, priority: Priority = "chunk") -> PacketItem:
    """A discovered chunk (SOURCE text). ``row`` has id, kind, title, text, occurred_at, source_item_id."""
    kind = {
        "email": "email excerpt",
        "calendar_event": "calendar event",
        "transcript": "transcript excerpt",
    }.get(row.kind, row.kind)
    title = row.title or "(no subject)"
    return PacketItem(
        key=f"chunk:{row.id}",
        kind="chunk",
        section="support",
        priority=priority,
        text=f"[{kind} · {delimit(title)} · {fmt_date(row.occurred_at, tz)}] {delimit(row.text)}",
        line=f"{title} ({fmt_date(row.occurred_at, tz)})",
        data_class="source",
        claim_kind="source",
        authority=3,
        entity_id=row.id,
        source_item_ids=(row.source_item_id,),
        as_of=row.occurred_at,
        score=score,
    )


def _event_detail(ev: TimelineEvent, persons: Mapping[UUID, PersonRef], self_id: UUID, tz: ZoneInfo) -> str:
    sets: dict[str, Any] = dict(ev.payload.get("set") or {})
    bits = []
    if "due_at" in sets or "due_text" in sets:
        due_at = sets.get("due_at")
        when = fmt_date(datetime.datetime.fromisoformat(due_at), tz) if isinstance(due_at, str) else "no date"
        bits.append(f"deadline {when}" + (f' ("{sets["due_text"]}")' if sets.get("due_text") else ""))
    if sets.get("reported_status"):
        bits.append(f"reported: {sets['reported_status']}")
    if "lifecycle_status" in sets and ev.actor == "user":
        bits.append(f"status {sets['lifecycle_status']}")
    if "verification_status" in sets and ev.actor == "user":
        bits.append(str(sets["verification_status"]))
    if "title" in sets and ev.event_type != "created":
        bits.append("title changed")
    by = ev.payload.get("by_person")
    if by:
        with contextlib.suppress(ValueError):
            bits.append(f"by {person_label(UUID(str(by)), persons, self_id)}")
    return "; ".join(bits)


def event_card(
    ev: TimelineEvent,
    persons: Mapping[UUID, PersonRef],
    self_id: UUID,
    tz: ZoneInfo,
    *,
    item_key: str,
    priority: Priority = "anchor_timeline",
    sources: tuple[UUID, ...] = (),
) -> PacketItem:
    data_class = {"user": "user", "model": "ai_derived"}.get(ev.actor, "computed")
    what = EVENT_TEXT.get(ev.event_type, ev.event_type.replace("_", " "))
    detail = _event_detail(ev, persons, self_id, tz)
    return PacketItem(
        key=f"event:{ev.id}",
        kind="event",
        section="timeline",
        priority=priority,
        text=f"[{fmt_date(ev.occurred_at, tz)} · {what} · {data_class}, authority {ev.authority}"
        + f" · {item_key}]"
        + (f" {delimit(detail)}" if detail else ""),
        line=f"{fmt_date(ev.occurred_at, tz)}: {what}" + (f" ({detail})" if detail else ""),
        data_class=data_class,
        claim_kind="user" if ev.actor == "user" else ("source" if ev.evidence_id else "inference"),
        authority=ev.authority,
        entity_id=ev.entity_id,
        source_item_ids=sources,
        evidence_ids=(ev.evidence_id,) if ev.evidence_id else (),
        as_of=ev.occurred_at,
        user_backed=ev.actor == "user",
    )
