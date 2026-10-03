"""Daily briefing (PRD §12, §30; AI_PIPELINE.md §4.2 O14; BACKEND_DESIGN.md §15, §16.8, §18).

Entirely deterministic: sections come from the same read models as Today, the headline from a
template, the email summary from counts. No model call (AI-12 is retired). One row per user and
local date, never regenerated; the "updated since briefing" banner is computed at read time.
Every entry keeps its entity ID and a provenance label, so AI-derived items stay labelled.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from eca import communication, identity, people, work
from eca.attention.events import BRIEFING_DUE, BriefingDue
from eca.attention.models import briefings_table
from eca.attention.reminder_rules import Calendar
from eca.attention.today import Today, build_today
from eca.platform.errors import NotFound
from eca.platform.events import NewEvent
from eca.platform.outbox import publish
from eca.platform.uow import UnitOfWork, UnitOfWorkFactory

log = structlog.get_logger("eca.attention.briefing")

PRIORITIES = 5
WAITING = 5
DEADLINES = 8
PEOPLE = 5
DEADLINE_DAYS = 3
MAIL_WINDOW = datetime.timedelta(hours=24)
ACTIVE_USER_WINDOW = datetime.timedelta(days=14)
SCHEDULE_LEAD = datetime.timedelta(hours=1)  # generated in the hour before work start
URGENT_SIGNALS = ("deadline", "explicit_urgency")
_LOCK_SQL = text("SELECT pg_advisory_xact_lock(hashtextextended('brief:' || :user || ':' || :day, 0))")


def item_label(v: work.WorkItemView) -> str:
    if v.origin == "user":
        return "added by you"
    if v.verification_status == "confirmed":
        return "AI-detected, confirmed by you"
    return f"AI suggestion{f', {v.confidence_band} confidence' if v.confidence_band else ''}"


def _person_name(pid: UUID | None, persons: dict[UUID, people.PersonRef]) -> str | None:
    p = persons.get(pid) if pid else None
    return (p.display_name or p.primary_email) if p else None


def _item_entry(v: work.WorkItemView, persons: dict[UUID, people.PersonRef], cal: Calendar) -> dict[str, Any]:
    counterpart = (
        v.owner_person_id if v.direction in ("waiting_for", "delegated") else v.counterparty_person_id
    )
    return {
        "kind": "work_item",
        "id": str(v.id),
        "title": v.title,
        "direction": v.direction,
        "person": _person_name(counterpart, persons),
        "due_at": v.due_at.isoformat() if v.due_at else None,
        "due_local": cal.local(v.due_at).date().isoformat() if v.due_at else None,
        "reasons": [r.get("text") for r in v.priority_reasons][:3],
        "data_class": "user" if v.origin == "user" or v.verification_status == "confirmed" else "ai_derived",
        "label": item_label(v),
        "source_item_ids": [str(s) for s in v.evidence_source_ids],
    }


def headline(titles: list[str]) -> str:
    """The template headline (AI_PIPELINE.md §4.2: "3 things need your attention today: …")."""
    if not titles:
        return "Nothing needs your attention today."
    noun = "thing needs" if len(titles) == 1 else "things need"
    shown = "; ".join(titles[:3]) + ("; …" if len(titles) > 3 else "")
    return f"{len(titles)} {noun} your attention today: {shown}."


async def build_content(uow: UnitOfWork, *, now: datetime.datetime) -> tuple[str, str, str, dict[str, Any]]:
    """(local date, timezone, headline, sections)."""
    t: Today = await build_today(uow, now=now)
    settings = await identity.get_user_settings(uow)
    cal = Calendar.of(settings.timezone, settings.work_hours)
    conversations = {c.id: c for c in t.needs_response}
    senders = await communication.latest_messages(uow, [c.id for c in t.needs_response])
    persons = dict(t.people)
    persons.update(
        await people.get_persons(uow, [m.sender_person_id for m in senders.values() if m.sender_person_id])
    )

    priorities: list[dict[str, Any]] = []
    for a in t.attention[:PRIORITIES]:
        if a["kind"] == "work_item" and a["id"] in t.attention_items:
            priorities.append(_item_entry(t.attention_items[a["id"]], persons, cal))
        elif a["kind"] == "conversation" and a["id"] in conversations:
            c = conversations[a["id"]]
            sender = senders.get(c.id)
            priorities.append(
                {
                    "kind": "conversation",
                    "id": str(c.id),
                    "title": f"Reply: {c.subject or '(no subject)'}",
                    "person": _person_name(sender.sender_person_id if sender else None, persons),
                    "reasons": [r.get("text") for r in c.priority_reasons][:3],
                    "data_class": "computed",
                    "label": "awaiting your reply"
                    + (" (AI triage)" if c.needs_reply_source == "triage" else ""),
                    "source_item_ids": [str(sender.source_item_id)] if sender else [],
                }
            )
    meetings = [
        {
            "id": str(m.id),
            "title": m.title or "(untitled)",
            "starts_at": m.starts_at.isoformat(),
            "local_time": cal.local(m.starts_at).strftime("%H:%M"),
            "data_class": "source",
        }
        for m in t.meetings
        if m.status != "cancelled"
    ]
    waiting = [_item_entry(v, persons, cal) for v in t.waiting_for[:WAITING]]
    horizon = now + datetime.timedelta(days=DEADLINE_DAYS)
    deadlines = [_item_entry(v, persons, cal) for v in t.deadlines if v.due_at and v.due_at <= horizon][
        :DEADLINES
    ]

    attention_people: list[dict[str, Any]] = []
    seen: set[UUID] = set()
    for c in t.needs_response:
        sender = senders.get(c.id)
        pid = sender.sender_person_id if sender else None
        if pid and pid not in seen:
            seen.add(pid)
            attention_people.append(
                {"id": str(pid), "name": _person_name(pid, persons), "reason": "awaiting your reply"}
            )
    for v in t.waiting_for:
        pid = v.owner_person_id
        if pid and pid not in seen and v.due_at and v.due_at < now:
            seen.add(pid)
            attention_people.append(
                {"id": str(pid), "name": _person_name(pid, persons), "reason": "overdue for you"}
            )
    attention_people = attention_people[:PEOPLE]

    counts = await communication.mail_counts(uow, start=now - MAIL_WINDOW, end=now)
    with_items, with_deadlines = await work.source_item_stats(uow, list(counts.inbound_source_ids))
    respond_today = sum(
        1
        for c in t.needs_response
        if any(s in URGENT_SIGNALS for s in (c.latest_triage or {}).get("urgency_signals", []) or [])
    )
    communication_section = {
        "window_start": (now - MAIL_WINDOW).isoformat(),
        "window_end": now.isoformat(),
        "received": counts.received,
        "require_attention": counts.require_attention,
        "with_action_items": len(with_items),
        "with_deadlines": len(with_deadlines),
        "respond_today": respond_today,
        "data_class": "computed",
    }
    content = {
        "priorities": priorities,
        "meetings": meetings,
        "waiting_on": waiting,
        "deadlines": deadlines,
        "people": attention_people,
        "communication": communication_section,
    }
    return t.date, t.timezone, headline([p["title"] for p in priorities]), content


@dataclass(frozen=True)
class BriefingView:
    date: datetime.date
    timezone: str
    headline: str
    content: dict[str, Any]
    generated_at: datetime.datetime
    trigger: str
    updated_since: dict[str, Any] | None


async def _lock(uow: UnitOfWork, day: str) -> None:
    await uow.session.execute(_LOCK_SQL, {"user": str(uow.user_id), "day": day})


async def generate(uow: UnitOfWork, *, now: datetime.datetime, trigger: str) -> bool:
    """Today's briefing (local date), once: insert ``ON CONFLICT DO NOTHING``. True when created."""
    day, tz, head, content = await build_content(uow, now=now)
    await _lock(uow, day)
    b = briefings_table
    created = (
        await uow.session.execute(
            insert(b)
            .values(
                user_id=uow.user_id,
                date=datetime.date.fromisoformat(day),
                timezone=tz,
                headline=head,
                content=content,
                generated_at=now,
                trigger=trigger,
            )
            .on_conflict_do_nothing(index_elements=["user_id", "date"])
            .returning(b.c.date)
        )
    ).scalar_one_or_none()
    return created is not None


async def _updated_since(
    uow: UnitOfWork, since: datetime.datetime, now: datetime.datetime
) -> dict[str, Any] | None:
    events = await work.events_recorded_between(uow, since, now, min_materiality=2, limit=500)
    newly_awaiting = [
        c
        for c in (await communication.needs_response_page(uow, after=None, limit=100)).items
        if c.last_inbound_at is not None and c.last_inbound_at > since
    ]
    changes = len(events) + len(newly_awaiting)
    return {"material_changes": changes, "since": since.isoformat()} if changes else None


async def get_briefing(uow: UnitOfWork, day: datetime.date, *, now: datetime.datetime) -> BriefingView:
    """The stored briefing; today's is generated on demand when the scheduled job has not run."""
    b = briefings_table
    stmt = select(b).where(b.c.user_id == uow.user_id, b.c.date == day)
    row = (await uow.session.execute(stmt)).one_or_none()
    if row is None:
        settings = await identity.get_user_settings(uow)
        today = Calendar.of(settings.timezone, settings.work_hours).local(now).date()
        if day != today:
            raise NotFound("no briefing for this date")
        await generate(uow, now=now, trigger="on_demand")
        row = (await uow.session.execute(stmt)).one()
    return BriefingView(
        row.date,
        row.timezone,
        row.headline,
        dict(row.content),
        row.generated_at,
        row.trigger,
        await _updated_since(uow, row.generated_at, now),
    )


async def schedule_user(uow: UnitOfWork, *, now: datetime.datetime) -> bool:
    """Publish ``BriefingDue`` in the hour before work start on a work day, for an active user
    (a session seen in the last 14 days) without today's briefing."""
    settings = await identity.get_user_settings(uow)
    cal = Calendar.of(settings.timezone, settings.work_hours)
    day = cal.local(now).date()
    start = cal.morning(day)
    if not cal.is_work_day(day) or not (start - SCHEDULE_LEAD <= now < start):
        return False
    seen = await identity.last_seen_at(uow)
    if seen is None or now - seen > ACTIVE_USER_WINDOW:
        return False
    b = briefings_table
    exists = (
        await uow.session.execute(select(b.c.date).where(b.c.user_id == uow.user_id, b.c.date == day))
    ).first()
    if exists is not None:
        return False
    await publish(
        uow,
        NewEvent(
            event_type=BRIEFING_DUE,
            aggregate_type="user",
            aggregate_id=settings.user_id,
            payload=BriefingDue(date=day),
        ),
    )
    return True


async def schedule_all(factory: UnitOfWorkFactory, *, now: datetime.datetime) -> int:
    async with factory(user_id=None) as uow:
        users = await identity.list_active_user_ids(uow)
    queued = 0
    for user_id in users:
        async with factory(user_id=user_id) as uow:
            queued += await schedule_user(uow, now=now)
    log.info("briefing_schedule", users=len(users), queued=queued)
    return queued


async def on_briefing_due(uow: UnitOfWork, day: datetime.date, *, now: datetime.datetime) -> bool:
    """``daily_briefing`` handler body: generate only for the user's current local date."""
    settings = await identity.get_user_settings(uow)
    if Calendar.of(settings.timezone, settings.work_hours).local(now).date() != day:
        return False
    return await generate(uow, now=now, trigger="schedule")
