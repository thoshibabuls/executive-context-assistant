"""``GET /api/v1/today`` read model (slice 1.8; PRD §9 Today dashboard).

Sections: attention (top items and conversations by priority), commitments (what the user
owes), waiting-for (what others owe), meetings today, deadlines (overdue and next 7 days),
needs-response, newly detected suggestions and (Phase 3) the active reminders, including those
delivered silently over the daily proactive cap (TECHNICAL_DESIGN.md §15.2: "others only in
Today"). Every item keeps its provenance fields
(``origin``, ``verification_status``, confidence band, evidence sources) so the page can label
AI suggestions as such. Bounded lists; one query per section; names resolved in one batch.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from eca import communication, meetings, people, work
from eca.attention.reminders import ReminderView, list_reminders
from eca.identity import get_user_settings
from eca.platform.uow import UnitOfWork

SECTION_LIMIT = 10
ATTENTION_LIMIT = 5
DEADLINE_HORIZON = datetime.timedelta(days=7)
NEW_SUGGESTIONS_WINDOW = datetime.timedelta(hours=24)


@dataclass(frozen=True)
class Today:
    date: str
    timezone: str
    attention: list[dict[str, Any]]
    commitments: list[work.WorkItemView]
    waiting_for: list[work.WorkItemView]
    deadlines: list[work.WorkItemView]
    new_suggestions: list[work.WorkItemView]
    needs_response: list[communication.ConversationSummary]
    meetings: list[meetings.MeetingView]
    people: dict[UUID, people.PersonRef]
    attention_items: dict[UUID, work.WorkItemView]
    reminders: list[ReminderView]


def _day_bounds(now: datetime.datetime, tz_name: str) -> tuple[datetime.datetime, datetime.datetime, str]:
    try:
        tz = ZoneInfo(tz_name)
    except (KeyError, ValueError):
        tz = ZoneInfo("UTC")
    local = now.astimezone(tz)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return (
        start.astimezone(datetime.UTC),
        (start + datetime.timedelta(days=1)).astimezone(datetime.UTC),
        (local.date().isoformat()),
    )


async def build_today(uow: UnitOfWork, *, now: datetime.datetime) -> Today:
    settings = await get_user_settings(uow)
    day_start, day_end, date = _day_bounds(now, settings.timezone)

    by_priority = await work.list_items_page(uow, status="open", sort="priority", limit=SECTION_LIMIT * 2)
    commitments = await work.list_items_page(uow, direction="my_commitment", sort="due", limit=SECTION_LIMIT)
    waiting = await work.list_items_page(uow, direction="waiting_for", sort="due", limit=SECTION_LIMIT)
    delegated = await work.list_items_page(uow, direction="delegated", sort="due", limit=SECTION_LIMIT)
    deadlines = await work.list_items_page(
        uow, due_before=now + DEADLINE_HORIZON, sort="due", limit=SECTION_LIMIT
    )
    suggested = await work.list_items_page(uow, verification="suggested", sort="created", limit=SECTION_LIMIT)
    needs_response = await communication.needs_response_page(uow, after=None, limit=SECTION_LIMIT)
    todays_meetings = await meetings.meetings_between(uow, day_start, day_end)

    attention: list[dict[str, Any]] = [
        {"kind": "work_item", "id": i.id, "score": i.priority_score or 0.0} for i in by_priority.items
    ] + [{"kind": "conversation", "id": c.id, "score": c.priority_score or 0.0} for c in needs_response.items]
    attention.sort(key=lambda a: (-a["score"], str(a["id"])))
    attention = [a for a in attention if a["score"] > 0][:ATTENTION_LIMIT]

    waiting_items = sorted(
        waiting.items + delegated.items,
        key=lambda i: (i.due_at is None, i.due_at or now, str(i.id)),
    )[:SECTION_LIMIT]
    new_items = [i for i in suggested.items if now - i.derived_at <= NEW_SUGGESTIONS_WINDOW]

    person_ids = {
        p
        for coll in (by_priority.items, commitments.items, waiting_items, deadlines.items, new_items)
        for i in coll
        for p in (i.owner_person_id, i.counterparty_person_id, i.requester_person_id)
        if p is not None
    }
    person_ids |= {p for m in todays_meetings for p in m.attendee_ids}
    persons = await people.get_persons(uow, sorted(person_ids))
    reminders, _ = await list_reminders(uow, state="active", after=None, limit=SECTION_LIMIT, now=now)
    return Today(
        date=date,
        timezone=settings.timezone,
        attention=attention,
        commitments=commitments.items,
        waiting_for=waiting_items,
        deadlines=deadlines.items,
        new_suggestions=new_items,
        needs_response=needs_response.items,
        meetings=todays_meetings,
        people=persons,
        attention_items={i.id: i for i in by_priority.items},
        reminders=reminders,
    )
