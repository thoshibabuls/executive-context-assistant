"""Meeting prep sections (slice 4.4; PRD §22; CONTEXT_ARCHITECTURE.md §10.4). Deterministic: no
model call (``attention`` never imports ``intelligence``).

Sections: purpose (calendar fields, SOURCE), what changed since the previous related meeting
(``work.changes_between``), open items in both directions with the attendees, unresolved questions
(open questions of prior related meetings and of threads with an attendee in the last 60 days)
and deadlines before the meeting. The cache key (BACKEND_DESIGN.md §18) is the SHA-256 of the
canonical JSON of the meeting ID and version, the prior meeting IDs, the sorted
``(type, id, version)`` of every included entity and the included changes; sections are stored
through ``meetings.store_prep_sections`` only when the key changed.

Jobs (BACKEND_DESIGN.md §15 Phase 4): ``MeetingChanged``, ``WorkItemChanged`` and
``MeetingProcessed`` recompute meetings starting within 24 h; ``prep_sweep`` (every 15 min)
covers meetings starting in the next 60 minutes (T-45).
"""

from __future__ import annotations

import datetime
import hashlib
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import structlog

from eca import communication, meetings, people, work
from eca.identity import list_active_user_ids
from eca.platform.uow import UnitOfWork, UnitOfWorkFactory

log = structlog.get_logger("eca.attention.prep")

PREP_HORIZON = datetime.timedelta(hours=24)
SWEEP_WINDOW = datetime.timedelta(minutes=60)
QUESTION_WINDOW = datetime.timedelta(days=60)
MINE = ("my_commitment", "my_task")
THEIRS = ("waiting_for", "delegated")
DESCRIPTION_CHARS = 600


@dataclass(frozen=True)
class PrepView:
    meeting_id: UUID
    key: str
    sections: dict[str, Any]
    empty: bool  # no item, question, deadline or change: no AI-11 call


def _iso(value: datetime.datetime | None) -> str | None:
    return value.isoformat() if value else None


def _item(i: work.WorkItemView, names: dict[UUID, str]) -> dict[str, Any]:
    return {
        "id": str(i.id),
        "title": i.title,
        "type": i.type,
        "direction": i.direction,
        "due_at": _iso(i.due_at),
        "due_text": i.due_text,
        "owner": names.get(i.owner_person_id) if i.owner_person_id else None,
        "counterparty": names.get(i.counterparty_person_id) if i.counterparty_person_id else None,
        "lifecycle_status": i.lifecycle_status,
        "reported_status": i.reported_status,
        "origin": i.origin,
        "verification_status": i.verification_status,
        "confidence_band": i.confidence_band,
        "version": i.version,
    }


def _question(d: work.DecisionView) -> dict[str, Any]:
    return {
        "id": str(d.id),
        "statement": d.statement,
        "meeting_id": str(d.meeting_id) if d.meeting_id else None,
        "conversation_id": str(d.conversation_id) if d.conversation_id else None,
        "origin": d.origin,
        "verification_status": d.verification_status,
        "confidence_band": d.confidence_band,
        "recorded_at": _iso(d.created_at),
        "version": d.version,
    }


def cache_key(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def compute_prep(uow: UnitOfWork, meeting_id: UUID, *, now: datetime.datetime) -> PrepView | None:
    m = await meetings.meeting_record(uow, meeting_id)
    if m is None:
        return None
    self_p = await people.get_self_person(uow)
    attendees = set(m.participant_ids) - {self_p.id}
    prior = await meetings.prior_meetings(uow, meeting_id, limit=2)
    involved = [
        i
        for i in await work.open_items(uow)
        if {p for p in (i.owner_person_id, i.counterparty_person_id, i.requester_person_id) if p} & attendees
    ]
    refs = await people.get_persons(
        uow,
        {p for i in involved for p in (i.owner_person_id, i.counterparty_person_id) if p},
    )
    names = {
        pid: ("you" if r.is_self else (r.display_name or r.primary_email or "unknown"))
        for pid, r in refs.items()
    }
    mine = [i for i in involved if i.direction in MINE]
    theirs = [i for i in involved if i.direction in THEIRS]
    threads = await communication.conversations_with_people(
        uow, sorted(attendees), since=now - QUESTION_WINDOW, limit=50
    )
    questions = await work.open_questions_for(
        uow, meeting_ids=[p.id for p in prior], conversation_ids=[t.id for t in threads], limit=20
    )
    deadlines = sorted(
        (i for i in mine + theirs if i.due_at is not None and i.due_at <= m.starts_at),
        key=lambda i: (i.due_at or m.starts_at, str(i.id)),
    )
    changes: list[work.MeetingChange] = []
    previous = prior[0] if prior else None
    if previous is not None:
        sources = [m.source_item_id, previous.source_item_id]
        for mid in (m.id, previous.id):
            rec = await meetings.recording_for_meeting(uow, mid)
            if rec is not None and rec.source_item_id is not None:
                sources.append(rec.source_item_id)
        changes = await work.changes_between(
            uow,
            since=previous.ends_at,
            until=min(m.starts_at, now),
            person_ids=attendees,
            source_item_ids=sources,
            meeting_ids=[m.id, previous.id],
        )
    changed_items = {
        v.id: v
        for v in await work.items_by_ids(uow, [c.entity_id for c in changes if c.entity_type == "work_item"])
    }
    changed_decisions = {
        d.id: d
        for d in await work.decisions_by_ids(
            uow, [c.entity_id for c in changes if c.entity_type == "decision"]
        )
    }
    what_changed = [
        {
            "entity_type": c.entity_type,
            "entity_id": str(c.entity_id),
            "kind": c.kind,
            "title": changed_items[c.entity_id].title
            if c.entity_id in changed_items
            else (changed_decisions[c.entity_id].statement if c.entity_id in changed_decisions else None),
            "recorded_at": _iso(c.recorded_at),
        }
        for c in changes[:20]
    ]
    sections: dict[str, Any] = {
        "purpose": {
            "title": m.title,
            "description": (m.description or "")[:DESCRIPTION_CHARS] or None,
            "starts_at": _iso(m.starts_at),
            "ends_at": _iso(m.ends_at),
            "project_hint": m.project_hint,
            "attendees": sorted(
                (r.display_name or r.primary_email or "unknown")
                for pid, r in (await people.get_persons(uow, attendees)).items()
            ),
        },
        "previous_meeting": (
            {"id": str(previous.id), "title": previous.title, "starts_at": _iso(previous.starts_at)}
            if previous
            else None
        ),
        "what_changed": what_changed,
        "open_items_mine": [_item(i, names) for i in mine[:20]],
        "open_items_theirs": [_item(i, names) for i in theirs[:20]],
        "unresolved_questions": [_question(q) for q in questions],
        "deadlines": [_item(i, names) for i in deadlines[:20]],
    }
    entities = sorted(
        [("work_item", str(i.id), i.version) for i in mine + theirs]
        + [("decision", str(q.id), q.version) for q in questions]
    )
    key = cache_key(
        {
            "meeting": [str(m.id), m.version],
            "prior": [str(p.id) for p in prior],
            "entities": entities,
            "changes": [
                [c["entity_type"], c["entity_id"], c["kind"], c["recorded_at"]] for c in what_changed
            ],
        }
    )
    empty = not (mine or theirs or questions or deadlines or what_changed)
    return PrepView(meeting_id=m.id, key=key, sections=sections, empty=empty)


async def store_prep(uow: UnitOfWork, view: PrepView, *, now: datetime.datetime) -> int:
    return await meetings.store_prep_sections(
        uow, view.meeting_id, key=view.key, sections=view.sections, computed_at=now
    )


async def refresh_meetings(uow: UnitOfWork, meeting_ids: list[UUID], *, now: datetime.datetime) -> int:
    """Recompute and store the sections of these meetings (each under its ``prep:`` lock)."""
    stored = 0
    for meeting_id in sorted(set(meeting_ids)):
        await meetings.lock_prep(uow, meeting_id)
        view = await compute_prep(uow, meeting_id, now=now)
        if view is not None:
            await store_prep(uow, view, now=now)
            stored += 1
    return stored


async def upcoming(
    uow: UnitOfWork,
    *,
    now: datetime.datetime,
    horizon: datetime.timedelta,
    person_ids: set[UUID] | None = None,
) -> list[UUID]:
    found = await meetings.meeting_details_between(
        uow, now, now + horizon, person_ids=sorted(person_ids) if person_ids else None, limit=50
    )
    return [m.id for m in found if m.starts_at > now and m.status != "cancelled"]


async def prep_sweep_all(uow_factory: UnitOfWorkFactory, *, now: datetime.datetime) -> None:
    """Every 15 minutes, one transaction per user: meetings starting in the next 60 minutes."""
    async with uow_factory(user_id=None) as uow:
        users = await list_active_user_ids(uow)
    total = 0
    for user_id in users:
        async with uow_factory(user_id=user_id) as uow:
            total += await refresh_meetings(uow, await upcoming(uow, now=now, horizon=SWEEP_WINDOW), now=now)
    if total:
        log.info("prep_sweep", meetings=total)
