"""Reminders: evaluation, delivery, user actions and the notification center
(TECHNICAL_DESIGN.md §15; BACKEND_DESIGN.md §10.1, §15, §16.8). Deterministic; no AI call.

- ``evaluate`` turns the rules of ``reminder_rules`` into ``pending`` rows: ``UNIQUE (user_id,
  fingerprint)`` makes it idempotent, so a delivered or dismissed reminder is never created again;
  pending rows whose material key is no longer current are cancelled.
- ``deliver`` (the 5-minute sweep) claims due rows with ``FOR UPDATE SKIP LOCKED``, cancels or
  suppresses stale ones, and delivers the rest in priority order: proactively (in-app
  notification, plus a Web Push row and ``ReminderDue``) while the day's cap allows, silently
  otherwise. Nothing is delivered during quiet hours.
- State transitions are conditional updates (``WHERE state IN (...)``).
Reminder text is rendered at read time from templates over the current item; rows hold no
message content.
"""

from __future__ import annotations

import datetime
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from eca import communication, meetings, people, work
from eca.attention.events import REMINDER_DUE, ReminderDue
from eca.attention.models import notifications_table, push_subscriptions_table, reminders_table
from eca.attention.reminder_rules import (
    MEETING_HORIZON,
    THEIRS,
    Calendar,
    Candidate,
    ItemFacts,
    Learning,
    MeetingFacts,
    ThreadFacts,
    follow_up_candidate,
    item_candidates,
    meeting_candidate,
    snooze_until,
)
from eca.identity import get_user_settings, list_active_user_ids
from eca.platform.errors import Conflict, NotFound, ValidationFailed
from eca.platform.events import NewEvent
from eca.platform.feedback import record_feedback
from eca.platform.ids import uuid7
from eca.platform.outbox import publish
from eca.platform.uow import UnitOfWork, UnitOfWorkFactory

log = structlog.get_logger("eca.attention.reminders")

DAILY_PROACTIVE_CAP = 5
DELIVERY_BATCH = 50
DISMISSAL_WINDOW = datetime.timedelta(days=90)
SNOOZE_WINDOW = datetime.timedelta(days=30)
FOLLOW_UP_SCAN = 50
STALE_SENDING = datetime.timedelta(minutes=10)
PUSH_MAX_ATTEMPTS = 3
ACTIVE_STATES = ("pending", "snoozed")
USER_OPEN_STATES = ("pending", "delivered", "snoozed")


async def calendar_of(uow: UnitOfWork) -> Calendar:
    settings = await get_user_settings(uow)
    return Calendar.of(settings.timezone, settings.work_hours)


async def learning_of(uow: UnitOfWork, *, now: datetime.datetime) -> tuple[Learning, set[bytes]]:
    """Bounded learning from the user's reminder history, and the dismissed material keys."""
    r = reminders_table
    dismissals = {
        (row.person_id, row.reminder_type): int(row.n)
        for row in await uow.session.execute(
            select(r.c.person_id, r.c.reminder_type, func.count().label("n"))
            .where(
                r.c.user_id == uow.user_id, r.c.state == "dismissed", r.c.closed_at >= now - DISMISSAL_WINDOW
            )
            .group_by(r.c.person_id, r.c.reminder_type)
        )
    }
    delays: dict[str, list[datetime.timedelta]] = defaultdict(list)
    for row in await uow.session.execute(
        select(r.c.reminder_type, r.c.snoozed_until, r.c.first_fire_at).where(
            r.c.user_id == uow.user_id,
            r.c.snooze_count > 0,
            r.c.snoozed_until.is_not(None),
            r.c.updated_at >= now - SNOOZE_WINDOW,
        )
    ):
        delays[row.reminder_type].append(row.snoozed_until - row.first_fire_at)
    dismissed = {
        bytes(row.material_key)
        for row in await uow.session.execute(
            select(r.c.material_key).where(
                r.c.user_id == uow.user_id, r.c.state == "dismissed", r.c.closed_at >= now - DISMISSAL_WINDOW
            )
        )
    }
    return Learning(dismissals, dict(delays)), dismissed


def item_facts(v: work.WorkItemView) -> ItemFacts:
    person = (
        v.owner_person_id if v.direction in THEIRS else (v.counterparty_person_id or v.requester_person_id)
    )
    return ItemFacts(
        id=v.id,
        direction=v.direction,
        due_at=v.due_at,
        due_precision=v.due_precision,
        due_kind=v.due_kind,
        lifecycle_status=v.lifecycle_status,
        verification_status=v.verification_status,
        confidence_band=v.confidence_band,
        priority=v.priority_score or 0.0,
        last_activity_at=v.last_activity_at,
        created_at=v.created_at,
        archived=v.archived,
        person_id=person,
    )


@dataclass
class EvaluationReport:
    created: int = 0
    cancelled: int = 0
    candidates: int = 0


async def _threads(
    uow: UnitOfWork, now: datetime.datetime, conversation_ids: Sequence[UUID] | None
) -> list[ThreadFacts]:
    quiet_since = now - datetime.timedelta(days=3)  # superset; the rule counts working days
    threads = await communication.waiting_on_others(uow, quiet_since=quiet_since, limit=FOLLOW_UP_SCAN)
    if conversation_ids is not None:
        wanted = set(conversation_ids)
        threads = [t for t in threads if t.id in wanted]
    senders = await communication.latest_messages(uow, [t.id for t in threads])
    return [
        ThreadFacts(
            id=t.id,
            last_message_at=t.last_message_at,
            last_inbound_at=t.last_inbound_at,
            priority=t.priority_score or 0.0,
            person_id=senders[t.id].sender_person_id if t.id in senders else None,
        )
        for t in threads
    ]


async def _meetings(
    uow: UnitOfWork,
    now: datetime.datetime,
    meeting_ids: Sequence[UUID] | None,
    items: list[work.WorkItemView],
) -> list[MeetingFacts]:
    upcoming = await meetings.meeting_details_between(uow, now, now + MEETING_HORIZON, limit=50)
    if meeting_ids is not None:
        wanted = set(meeting_ids)
        upcoming = [m for m in upcoming if m.id in wanted]
    if not upcoming:
        return []
    self_id = (await people.get_self_person(uow)).id
    out = []
    for m in upcoming:
        attendees = set(m.attendee_ids) - {self_id}
        involved = [
            i
            for i in items
            if attendees
            & {p for p in (i.owner_person_id, i.counterparty_person_id, i.requester_person_id) if p}
        ]
        out.append(
            MeetingFacts(
                id=m.id,
                starts_at=m.starts_at,
                status=m.status,
                open_items=len(involved),
                priority=max((i.priority_score or 0.0 for i in involved), default=0.0),
            )
        )
    return out


async def evaluate(
    uow: UnitOfWork,
    *,
    now: datetime.datetime,
    item_ids: Sequence[UUID] | None = None,
    conversation_ids: Sequence[UUID] | None = None,
    meeting_ids: Sequence[UUID] | None = None,
    full: bool = False,
) -> EvaluationReport:
    """Evaluate the rules for the given entities (event handlers) or for everything (sweep)."""
    cal = await calendar_of(uow)
    learning, dismissed = await learning_of(uow, now=now)
    candidates: list[Candidate] = []
    scope: set[tuple[str, UUID]] = set()
    all_items: list[work.WorkItemView] | None = None
    if full or item_ids:
        items = await work.open_items(uow, None if full else list(item_ids or ()))
        all_items = items if full else None
        for v in items:
            candidates.extend(item_candidates(item_facts(v), now, cal, learning))
        scope |= {("work_item", i) for i in (item_ids or ())}
    if full or conversation_ids:
        for thread in await _threads(uow, now, None if full else list(conversation_ids or ())):
            c = follow_up_candidate(thread, now, cal, learning)
            if c is not None:
                candidates.append(c)
        scope |= {("conversation", i) for i in (conversation_ids or ())}
    if full or meeting_ids:
        if all_items is None:
            all_items = await work.open_items(uow)
        for m in await _meetings(uow, now, None if full else list(meeting_ids or ()), all_items):
            c = meeting_candidate(m, now)
            if c is not None:
                candidates.append(c)
        scope |= {("meeting", i) for i in (meeting_ids or ())}
    report = EvaluationReport(candidates=len(candidates))
    r = reminders_table
    for c in candidates:
        if c.material_key in dismissed:
            continue  # a dismissal suppresses every slot until the facts change (§15.2)
        inserted = (
            await uow.session.execute(
                insert(r)
                .values(
                    id=uuid7(),
                    user_id=uow.user_id,
                    item_type=c.item_type,
                    item_id=c.item_id,
                    person_id=c.person_id,
                    reminder_type=c.reminder_type,
                    slot=c.slot,
                    fire_at=c.fire_at,
                    first_fire_at=c.fire_at,
                    fingerprint=c.fingerprint,
                    material_key=c.material_key,
                    reason=c.reason,
                    priority=c.priority,
                    proactive_eligible=c.proactive_eligible,
                )
                .on_conflict_do_nothing(index_elements=["user_id", "fingerprint"])
                .returning(r.c.id)
            )
        ).scalar_one_or_none()
        report.created += inserted is not None
    current = {(c.item_type, c.item_id, c.reminder_type, c.material_key, c.slot) for c in candidates}
    stmt = select(r.c.id, r.c.item_type, r.c.item_id, r.c.reminder_type, r.c.material_key, r.c.slot).where(
        r.c.user_id == uow.user_id, r.c.state == "pending"
    )
    if not full:
        if not scope:
            return report
        stmt = stmt.where(
            or_(*[and_(r.c.item_type == kind, r.c.item_id == entity) for kind, entity in sorted(scope)])
        )
    stale = [
        row.id
        for row in await uow.session.execute(stmt)
        if (row.item_type, row.item_id, row.reminder_type, bytes(row.material_key), row.slot) not in current
    ]
    report.cancelled = await _close(
        uow, stale, "cancelled", "no_longer_applies", now=now, from_states=("pending",)
    )
    return report


async def _close(
    uow: UnitOfWork,
    ids: Sequence[UUID],
    state: str,
    reason: str,
    *,
    now: datetime.datetime,
    from_states: Sequence[str] = ACTIVE_STATES,
) -> int:
    if not ids:
        return 0
    r = reminders_table
    result = await uow.session.execute(
        update(r)
        .where(r.c.user_id == uow.user_id, r.c.id.in_(list(ids)), r.c.state.in_(list(from_states)))
        .values(state=state, closed_at=now, closed_reason=reason, version=r.c.version + 1)
    )
    return int(result.rowcount)  # type: ignore[attr-defined]


@dataclass
class DeliveryReport:
    delivered: int = 0
    proactive: int = 0
    suppressed: int = 0
    cancelled: int = 0
    quiet_hours: bool = False
    pushes: list[tuple[UUID, int]] = field(default_factory=list)


async def _stale_and_acted(
    uow: UnitOfWork, rows: Sequence[Any], now: datetime.datetime
) -> tuple[set[UUID], set[UUID]]:
    """(reminders to cancel, reminders to suppress as acted upon)."""
    cancel: set[UUID] = set()
    acted: set[UUID] = set()
    by_kind: dict[str, list[Any]] = defaultdict(list)
    for row in rows:
        by_kind[row.item_type].append(row)
    if by_kind["work_item"]:
        live = {i.id for i in await work.open_items(uow, [row.item_id for row in by_kind["work_item"]])}
        since: dict[UUID, datetime.datetime] = {}
        for row in by_kind["work_item"]:
            if row.item_id not in live:
                cancel.add(row.id)
            else:
                at = row.delivered_at or row.created_at
                since[row.item_id] = min(since.get(row.item_id, at), at)
        touched = await work.user_activity_since(uow, "work_item", since)
        acted |= {row.id for row in by_kind["work_item"] if row.item_id in touched and row.id not in cancel}
    if by_kind["conversation"]:
        convs = {
            c.id: c
            for c in await communication.conversations_by_ids(
                uow, [row.item_id for row in by_kind["conversation"]]
            )
        }
        for row in by_kind["conversation"]:
            c = convs.get(row.item_id)
            if c is None or c.awaiting != "other" or c.handled_by_user_at is not None:
                cancel.add(row.id)
            elif c.last_message_at is not None and c.last_message_at > (row.delivered_at or row.created_at):
                acted.add(row.id)  # the user wrote again, or a reply arrived
    if by_kind["meeting"]:
        found = {
            m.id: m
            for m in await meetings.get_meeting_details(uow, [row.item_id for row in by_kind["meeting"]])
        }
        for row in by_kind["meeting"]:
            m = found.get(row.item_id)
            if m is None or m.status == "cancelled" or m.starts_at <= now:
                cancel.add(row.id)
    return cancel, acted


async def deliver(
    uow: UnitOfWork, *, now: datetime.datetime, cap: int = DAILY_PROACTIVE_CAP
) -> DeliveryReport:
    report = DeliveryReport()
    cal = await calendar_of(uow)
    if cal.in_quiet_hours(now):
        report.quiet_hours = True
        return report
    r, n = reminders_table, notifications_table
    rows = (
        await uow.session.execute(
            select(r)
            .where(r.c.user_id == uow.user_id, r.c.state.in_(ACTIVE_STATES), r.c.fire_at <= now)
            .order_by(r.c.priority.desc(), r.c.fire_at, r.c.id)
            .limit(DELIVERY_BATCH)
            .with_for_update(skip_locked=True)
        )
    ).all()
    if not rows:
        return report
    cancel, acted = await _stale_and_acted(uow, rows, now)
    report.cancelled = await _close(uow, sorted(cancel), "cancelled", "item_closed", now=now)
    report.suppressed = await _close(uow, sorted(acted - cancel), "suppressed", "acted_upon", now=now)
    sent_today = (
        await uow.session.execute(
            select(func.count()).where(
                r.c.user_id == uow.user_id, r.c.proactive.is_(True), r.c.delivered_at >= cal.day_start(now)
            )
        )
    ).scalar_one()
    has_push = (
        await uow.session.execute(
            select(func.count()).where(
                push_subscriptions_table.c.user_id == uow.user_id,
                push_subscriptions_table.c.revoked_at.is_(None),
            )
        )
    ).scalar_one() > 0
    for row in rows:
        if row.id in cancel or row.id in acted:
            continue
        proactive = bool(row.proactive_eligible) and sent_today < cap
        seq = (
            await uow.session.execute(
                update(r)
                .where(r.c.id == row.id, r.c.state.in_(ACTIVE_STATES))
                .values(
                    state="delivered",
                    delivery_seq=r.c.delivery_seq + 1,
                    delivered_at=now,
                    proactive=proactive,
                    version=r.c.version + 1,
                )
                .returning(r.c.delivery_seq)
            )
        ).scalar_one_or_none()
        if seq is None:
            continue
        report.delivered += 1
        if not proactive:
            continue
        sent_today += 1
        report.proactive += 1
        await uow.session.execute(
            insert(n)
            .values(
                id=uuid7(),
                user_id=uow.user_id,
                reminder_id=row.id,
                channel="in_app",
                seq=seq,
                state="sent",
                sent_at=now,
            )
            .on_conflict_do_nothing(index_elements=["reminder_id", "channel", "seq"])
        )
        if has_push:
            created = (
                await uow.session.execute(
                    insert(n)
                    .values(
                        id=uuid7(),
                        user_id=uow.user_id,
                        reminder_id=row.id,
                        channel="web_push",
                        seq=seq,
                        state="pending",
                    )
                    .on_conflict_do_nothing(index_elements=["reminder_id", "channel", "seq"])
                    .returning(n.c.id)
                )
            ).scalar_one_or_none()
            if created is not None:
                await _publish_push(uow, row.id, seq)
                report.pushes.append((row.id, seq))
    return report


async def _publish_push(uow: UnitOfWork, reminder_id: UUID, seq: int) -> None:
    await publish(
        uow,
        NewEvent(
            event_type=REMINDER_DUE,
            aggregate_type="reminder",
            aggregate_id=reminder_id,
            payload=ReminderDue(reminder_id=reminder_id, seq=seq),
        ),
    )


async def retry_stale_pushes(uow: UnitOfWork, *, now: datetime.datetime) -> int:
    """``sending`` pushes older than 10 minutes (a crash after the claim): back to ``pending`` and
    sent again, at most 3 attempts in total; then ``failed`` (BACKEND_DESIGN.md §10.1)."""
    n = notifications_table
    rows = (
        await uow.session.execute(
            select(n.c.id, n.c.reminder_id, n.c.seq, n.c.attempts)
            .where(
                n.c.user_id == uow.user_id,
                n.c.channel == "web_push",
                n.c.state == "sending",
                n.c.updated_at < now - STALE_SENDING,
            )
            .with_for_update(skip_locked=True)
        )
    ).all()
    retried = 0
    for row in rows:
        if row.attempts >= PUSH_MAX_ATTEMPTS:
            await uow.session.execute(
                update(n)
                .where(n.c.id == row.id, n.c.state == "sending")
                .values(state="failed", last_error="attempts")
            )
            continue
        await uow.session.execute(
            update(n).where(n.c.id == row.id, n.c.state == "sending").values(state="pending")
        )
        await _publish_push(uow, row.reminder_id, row.seq)
        retried += 1
    return retried


async def sweep_user(uow: UnitOfWork, *, now: datetime.datetime, evaluate_rules: bool) -> DeliveryReport:
    if evaluate_rules:
        await evaluate(uow, now=now, full=True)
    report = await deliver(uow, now=now)
    await retry_stale_pushes(uow, now=now)
    return report


async def sweep_all(factory: UnitOfWorkFactory, *, now: datetime.datetime) -> int:
    """The 5-minute ``reminder_sweep`` (lock ``reminder_sweep``): one transaction per user; the
    full rule evaluation runs on the tick in the first 5 minutes of each hour."""
    evaluate_rules = now.astimezone(datetime.UTC).minute < 5
    async with factory(user_id=None) as uow:
        users = await list_active_user_ids(uow)
    delivered = 0
    for user_id in users:
        async with factory(user_id=user_id) as uow:
            delivered += (await sweep_user(uow, now=now, evaluate_rules=evaluate_rules)).delivered
    log.info("reminder_sweep", users=len(users), delivered=delivered, evaluated=evaluate_rules)
    return delivered


# ---------------------------------------------------------------- rendering and user actions


@dataclass(frozen=True)
class ReminderView:
    id: UUID
    item_type: str
    item_id: UUID
    reminder_type: str
    state: str
    text: str
    fire_at: datetime.datetime
    delivered_at: datetime.datetime | None
    snoozed_until: datetime.datetime | None
    proactive: bool | None
    priority: float
    reason: dict[str, Any]
    provenance: dict[str, Any] | None  # the underlying item's origin and evidence (AI_PIPELINE.md §5.1)
    version: int


def _date(moment: datetime.datetime | None, cal: Calendar, *, with_time: bool = False) -> str:
    if moment is None:
        return "an unknown date"
    local = cal.local(moment)
    text = f"{local.strftime('%a')} {local.day} {local.strftime('%b')}"
    return f"{text} {local.strftime('%H:%M')}" if with_time else text


def _when(due: datetime.datetime | None, now: datetime.datetime, cal: Calendar) -> str:
    if due is None:
        return ""
    day, today = cal.local(due).date(), cal.local(now).date()
    if day == today:
        return "today"
    if day == today + datetime.timedelta(days=1):
        return "tomorrow"
    return f"on {_date(due, cal)}"


def render(
    reminder_type: str,
    *,
    title: str,
    person: str | None,
    due_at: datetime.datetime | None,
    starts_at: datetime.datetime | None,
    open_items: int,
    now: datetime.datetime,
    cal: Calendar,
) -> str:
    """Fixed templates (PRD §19); the title is the user's own item title, quoted."""
    quoted = f"“{title}”"
    if reminder_type == "deadline":
        return f"{quoted} is due {_when(due_at, now, cal)}."
    if reminder_type == "overdue":
        return f"{quoted} is overdue (it was due {_when(due_at, now, cal)})."
    if reminder_type == "commitment":
        return f"You committed to {quoted}, due {_when(due_at, now, cal)}."
    if reminder_type == "waiting_for":
        who = person or "They"
        due = f" (due {_when(due_at, now, cal)})" if due_at else ""
        return f"{who} has not yet delivered {quoted}{due}."
    if reminder_type == "follow_up":
        who = f" from {person}" if person else ""
        return f"No reply yet{who} on {quoted}."
    if reminder_type == "meeting_prep":
        start = _date(starts_at, cal, with_time=True)
        return f"{quoted} starts at {start}. {open_items} open item(s) with the attendees."
    return quoted


def _item_provenance(v: work.WorkItemView) -> dict[str, Any]:
    return {
        "origin": v.origin,
        "verification_status": v.verification_status,
        "confidence_band": v.confidence_band,
        "extraction_method": v.extraction_method,
        "evidence_source_ids": [str(s) for s in v.evidence_source_ids],
    }


async def views_of(uow: UnitOfWork, rows: Sequence[Any], *, now: datetime.datetime) -> list[ReminderView]:
    cal = await calendar_of(uow)
    item_ids = [r.item_id for r in rows if r.item_type == "work_item"]
    items = {i.id: i for i in await work.items_by_ids(uow, item_ids, include_rejected=True)}
    convs = {
        c.id: c
        for c in await communication.conversations_by_ids(
            uow, [r.item_id for r in rows if r.item_type == "conversation"]
        )
    }
    held = {
        m.id: m
        for m in await meetings.get_meeting_details(
            uow, [r.item_id for r in rows if r.item_type == "meeting"]
        )
    }
    persons = await people.get_persons(uow, [r.person_id for r in rows if r.person_id])
    out = []
    for r in rows:
        person = persons.get(r.person_id) if r.person_id else None
        name = (person.display_name or person.primary_email) if person else None
        item = items.get(r.item_id)
        title, due, starts, provenance = "an item", None, None, None
        if item is not None:
            title, due, provenance = item.title, item.due_at, _item_provenance(item)
        elif r.item_type == "conversation" and r.item_id in convs:
            title = convs[r.item_id].subject or "(no subject)"
        elif r.item_type == "meeting" and r.item_id in held:
            title, starts = held[r.item_id].title or "Meeting", held[r.item_id].starts_at
        text = render(
            r.reminder_type,
            title=title,
            person=name,
            due_at=due,
            starts_at=starts,
            open_items=int((r.reason or {}).get("open_items", 0)),
            now=now,
            cal=cal,
        )
        out.append(
            ReminderView(
                r.id,
                r.item_type,
                r.item_id,
                r.reminder_type,
                r.state,
                text,
                r.fire_at,
                r.delivered_at,
                r.snoozed_until,
                r.proactive,
                r.priority,
                dict(r.reason or {}),
                provenance,
                r.version,
            )
        )
    return out


STATE_FILTERS = {"active": ("delivered", "snoozed"), "pending": ("pending",), "all": None}


async def list_reminders(
    uow: UnitOfWork, *, state: str, after: list[Any] | None, limit: int, now: datetime.datetime
) -> tuple[list[ReminderView], list[Any] | None]:
    """Newest first by fire time, keyset ``[fire_at, id]``."""
    if state not in STATE_FILTERS:
        raise ValidationFailed("state must be active, pending or all")
    r = reminders_table
    stmt = select(r).where(r.c.user_id == uow.user_id)
    states = STATE_FILTERS[state]
    if states is not None:
        stmt = stmt.where(r.c.state.in_(states))
    if after is not None:
        t0, id0 = datetime.datetime.fromisoformat(after[0]), UUID(after[1])
        stmt = stmt.where(or_(r.c.fire_at < t0, and_(r.c.fire_at == t0, r.c.id > id0)))
    rows = (await uow.session.execute(stmt.order_by(r.c.fire_at.desc(), r.c.id).limit(limit + 1))).all()
    page = rows[:limit]
    next_key = [page[-1].fire_at.isoformat(), str(page[-1].id)] if len(rows) > limit and page else None
    return await views_of(uow, page, now=now), next_key


async def get_reminder(uow: UnitOfWork, reminder_id: UUID, *, now: datetime.datetime) -> ReminderView:
    r = reminders_table
    row = (
        await uow.session.execute(select(r).where(r.c.user_id == uow.user_id, r.c.id == reminder_id))
    ).one_or_none()
    if row is None:
        raise NotFound("reminder not found")
    return (await views_of(uow, [row], now=now))[0]


async def _transition(
    uow: UnitOfWork,
    reminder_id: UUID,
    *,
    target: str,
    values: dict[str, Any],
    now: datetime.datetime,
    feedback: dict[str, Any],
) -> ReminderView:
    r = reminders_table
    cur = (
        await uow.session.execute(
            select(r.c.state).where(r.c.user_id == uow.user_id, r.c.id == reminder_id).with_for_update()
        )
    ).one_or_none()
    if cur is None:
        raise NotFound("reminder not found")
    if cur.state == target and target != "snoozed":
        return await get_reminder(uow, reminder_id, now=now)  # repeating the command is a no-op
    changed = (
        await uow.session.execute(
            update(r)
            .where(r.c.id == reminder_id, r.c.state.in_(USER_OPEN_STATES))
            .values(state=target, version=r.c.version + 1, **values)
            .returning(r.c.id)
        )
    ).scalar_one_or_none()
    if changed is None:
        raise Conflict(f"a {cur.state} reminder cannot be changed")
    await record_feedback(
        uow,
        target_type="reminder",
        target_id=reminder_id,
        action=target,
        before={"state": cur.state},
        after=feedback,
    )
    return await get_reminder(uow, reminder_id, now=now)


async def snooze(
    uow: UnitOfWork,
    reminder_id: UUID,
    *,
    preset: str | None,
    until: datetime.datetime | None,
    now: datetime.datetime,
) -> ReminderView:
    cal = await calendar_of(uow)
    end = snooze_until(now, cal, preset=preset, until=until)
    if end is None:
        raise ValidationFailed("snooze needs a preset (1h, 3h, tomorrow) or a time within 7 days")
    r = reminders_table
    return await _transition(
        uow,
        reminder_id,
        target="snoozed",
        values={"fire_at": end, "snoozed_until": end, "snooze_count": r.c.snooze_count + 1},
        now=now,
        feedback={"until": end.isoformat()},
    )


async def dismiss(uow: UnitOfWork, reminder_id: UUID, *, now: datetime.datetime) -> ReminderView:
    return await _transition(
        uow,
        reminder_id,
        target="dismissed",
        values={"closed_at": now, "closed_reason": "user"},
        now=now,
        feedback={},
    )


async def mark_acted(uow: UnitOfWork, reminder_id: UUID, *, now: datetime.datetime) -> ReminderView:
    return await _transition(
        uow,
        reminder_id,
        target="acted",
        values={"closed_at": now, "closed_reason": "user"},
        now=now,
        feedback={},
    )


@dataclass(frozen=True)
class NotificationView:
    id: UUID
    reminder: ReminderView
    created_at: datetime.datetime
    read_at: datetime.datetime | None


async def list_notifications(
    uow: UnitOfWork, *, unread: bool, after: list[Any] | None, limit: int, now: datetime.datetime
) -> tuple[list[NotificationView], list[Any] | None]:
    """The in-app notification center, newest first (keyset ``[created_at, id]``)."""
    n, r = notifications_table, reminders_table
    stmt = (
        select(n.c.id.label("notification_id"), n.c.created_at.label("n_created"), n.c.read_at, r)
        .join(r, r.c.id == n.c.reminder_id)
        .where(n.c.user_id == uow.user_id, n.c.channel == "in_app")
    )
    if unread:
        stmt = stmt.where(n.c.read_at.is_(None))
    if after is not None:
        t0, id0 = datetime.datetime.fromisoformat(after[0]), UUID(after[1])
        stmt = stmt.where(or_(n.c.created_at < t0, and_(n.c.created_at == t0, n.c.id > id0)))
    rows = (await uow.session.execute(stmt.order_by(n.c.created_at.desc(), n.c.id).limit(limit + 1))).all()
    page = rows[:limit]
    views = await views_of(uow, page, now=now)
    out = [
        NotificationView(row.notification_id, v, row.n_created, row.read_at)
        for row, v in zip(page, views, strict=True)
    ]
    last = page[-1] if page else None
    next_key = [last.n_created.isoformat(), str(last.notification_id)] if len(rows) > limit and last else None
    return out, next_key


async def mark_read(
    uow: UnitOfWork, *, ids: Sequence[UUID] | None, before: datetime.datetime | None, now: datetime.datetime
) -> int:
    n = notifications_table
    stmt = update(n).where(n.c.user_id == uow.user_id, n.c.channel == "in_app", n.c.read_at.is_(None))
    if ids:
        stmt = stmt.where(n.c.id.in_(list(ids)))
    elif before is not None:
        stmt = stmt.where(n.c.created_at <= before)
    else:
        raise ValidationFailed("give ids or before")
    result = await uow.session.execute(stmt.values(read_at=now))
    return int(result.rowcount)  # type: ignore[attr-defined]
