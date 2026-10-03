"""Reminder rules: pure, deterministic functions (TECHNICAL_DESIGN.md §15.1-§15.2, §15.4).

No database and no clock: callers pass ``now`` and the user's calendar. No model decides whether
to remind. Each rule turns the facts of one item, thread or meeting into candidates with a slot,
a fire time, a material key and a fingerprint; storage, suppression and delivery are in
``reminders``.
"""

from __future__ import annotations

import datetime
import hashlib
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_WORK_DAYS = (0, 1, 2, 3, 4)  # Monday-Friday, as coverage (CONTEXT_ARCHITECTURE.md §9.10)
DEFAULT_WORK_START = 9
DEFAULT_WORK_END = 18
DEFAULT_QUIET_START = 21
DEFAULT_QUIET_END = 7

DEADLINE_THRESHOLD = 40.0
DISMISSAL_STEP = 10.0
DEADLINE_THRESHOLD_MAX = 70.0
SUPPRESS_AFTER_DISMISSALS = 3
OVERDUE_EVERY_WORKING_DAYS = 2
WAITING_QUIET_WORKING_DAYS = 5
FOLLOW_UP_WORKING_DAYS = 3
MEETING_PREP_LEAD = datetime.timedelta(minutes=30)
MEETING_HORIZON = datetime.timedelta(hours=24)
QUESTION_WINDOW = datetime.timedelta(days=60)
SNOOZE_LEARN_MIN = 3
SNOOZE_SHIFT_MAX = datetime.timedelta(hours=4)
OPEN = ("open", "in_progress")
MINE = ("my_task", "my_commitment")
THEIRS = ("waiting_for", "delegated")
HARD_DUE_KINDS = ("by", "on")
HARD_PRECISIONS = ("day", "datetime")


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


@dataclass(frozen=True)
class Calendar:
    """The user's local time rules: work hours, working days and quiet hours (§15.4)."""

    tz: ZoneInfo
    days: tuple[int, ...] = DEFAULT_WORK_DAYS
    start_hour: int = DEFAULT_WORK_START
    end_hour: int = DEFAULT_WORK_END
    quiet_start: int = DEFAULT_QUIET_START
    quiet_end: int = DEFAULT_QUIET_END

    @classmethod
    def of(cls, timezone: str, work_hours: Mapping[str, Any]) -> Calendar:
        def hour(key: str, default: int) -> int:
            try:
                value = int(work_hours.get(key, default))
            except (TypeError, ValueError):
                return default
            return value if 0 <= value <= 23 else default

        days = tuple(int(d) for d in work_hours.get("days", DEFAULT_WORK_DAYS) if 0 <= int(d) <= 6)
        return cls(
            tz=_zone(timezone),
            days=days or DEFAULT_WORK_DAYS,
            start_hour=hour("start_hour", DEFAULT_WORK_START),
            end_hour=hour("end_hour", DEFAULT_WORK_END),
            quiet_start=hour("quiet_start_hour", DEFAULT_QUIET_START),
            quiet_end=hour("quiet_end_hour", DEFAULT_QUIET_END),
        )

    def local(self, moment: datetime.datetime) -> datetime.datetime:
        return moment.astimezone(self.tz)

    def at_hour(self, day: datetime.date, hour: int) -> datetime.datetime:
        return datetime.datetime.combine(day, datetime.time(hour), tzinfo=self.tz).astimezone(datetime.UTC)

    def morning(self, day: datetime.date) -> datetime.datetime:
        """Work start of a local date (UTC)."""
        return self.at_hour(day, self.start_hour)

    def is_work_day(self, day: datetime.date) -> bool:
        return day.weekday() in self.days

    def in_work_hours(self, moment: datetime.datetime) -> bool:
        local = self.local(moment)
        return self.is_work_day(local.date()) and self.start_hour <= local.hour < self.end_hour

    def next_work_slot(self, moment: datetime.datetime) -> datetime.datetime:
        """``moment`` inside work hours, else the next work start."""
        if self.in_work_hours(moment):
            return moment
        day = self.local(moment).date()
        if self.is_work_day(day) and self.local(moment).hour < self.start_hour:
            return self.morning(day)
        return self.next_work_morning(day)

    def next_work_morning(self, day: datetime.date) -> datetime.datetime:
        """Work start of the first working day after ``day``."""
        nxt = day + datetime.timedelta(days=1)
        for _ in range(14):
            if self.is_work_day(nxt):
                return self.morning(nxt)
            nxt += datetime.timedelta(days=1)
        return self.morning(day + datetime.timedelta(days=1))

    def add_working_days(self, moment: datetime.datetime, n: int) -> datetime.datetime:
        """The same local time ``n`` working days later."""
        local = self.local(moment)
        left = n
        while left > 0:
            local += datetime.timedelta(days=1)
            if self.is_work_day(local.date()):
                left -= 1
        return local.astimezone(datetime.UTC)

    def working_days_between(self, start: datetime.datetime, end: datetime.datetime) -> int:
        """Whole working days from ``start`` to ``end`` (0 when ``end`` ≤ ``start``)."""
        if end <= start:
            return 0
        count, day, last = 0, self.local(start).date(), self.local(end).date()
        while day < last:
            day += datetime.timedelta(days=1)
            if self.is_work_day(day):
                count += 1
        return count

    def in_quiet_hours(self, moment: datetime.datetime) -> bool:
        hour = self.local(moment).hour
        if self.quiet_start == self.quiet_end:
            return False
        if self.quiet_start > self.quiet_end:  # crosses midnight (default 21:00-07:00)
            return hour >= self.quiet_start or hour < self.quiet_end
        return self.quiet_start <= hour < self.quiet_end

    def day_start(self, moment: datetime.datetime) -> datetime.datetime:
        return self.at_hour(self.local(moment).date(), 0)


def material_key(*parts: object) -> bytes:
    """SHA-256 over the facts whose change makes a reminder new (§15.2, §15.4)."""
    text = "|".join(
        "" if p is None else (p.isoformat() if isinstance(p, datetime.datetime) else str(p)) for p in parts
    )
    return hashlib.sha256(text.encode()).digest()


def fingerprint(key: bytes, slot: str) -> bytes:
    return hashlib.sha256(key + b"|" + slot.encode()).digest()


@dataclass(frozen=True)
class Learning:
    """Bounded per-user learning from the user's own reminder history (TECHNICAL_DESIGN.md §12.7)."""

    dismissals: Mapping[tuple[UUID | None, str], int] = field(default_factory=dict)  # (person, type)
    snooze_delays: Mapping[str, Sequence[datetime.timedelta]] = field(default_factory=dict)  # type → delays

    def deadline_threshold(self, person_id: UUID | None) -> float:
        n = sum(v for (p, _), v in self.dismissals.items() if p is not None and p == person_id)
        return min(DEADLINE_THRESHOLD + DISMISSAL_STEP * n, DEADLINE_THRESHOLD_MAX)

    def suppressed(self, person_id: UUID | None, reminder_type: str) -> bool:
        return (
            person_id is not None
            and self.dismissals.get((person_id, reminder_type), 0) >= SUPPRESS_AFTER_DISMISSALS
        )

    def shift(self, reminder_type: str) -> datetime.timedelta:
        delays = [d for d in self.snooze_delays.get(reminder_type, ()) if d > datetime.timedelta(0)]
        if len(delays) < SNOOZE_LEARN_MIN:
            return datetime.timedelta(0)
        median = datetime.timedelta(seconds=statistics.median(d.total_seconds() for d in delays))
        return min(max(median, datetime.timedelta(0)), SNOOZE_SHIFT_MAX)


@dataclass(frozen=True)
class Candidate:
    item_type: str
    item_id: UUID
    reminder_type: str
    slot: str
    fire_at: datetime.datetime
    material_key: bytes
    priority: float
    proactive_eligible: bool
    person_id: UUID | None
    reason: dict[str, Any]

    @property
    def fingerprint(self) -> bytes:
        return fingerprint(self.material_key, self.slot)


@dataclass(frozen=True)
class ItemFacts:
    id: UUID
    direction: str
    due_at: datetime.datetime | None
    due_precision: str | None
    due_kind: str | None
    lifecycle_status: str
    verification_status: str
    confidence_band: str | None
    priority: float
    last_activity_at: datetime.datetime | None
    created_at: datetime.datetime | None
    archived: bool
    person_id: UUID | None  # the counterpart: owner for waiting/delegated, else counterparty/requester


def _later(
    fire_at: datetime.datetime, shift: datetime.timedelta, due_at: datetime.datetime | None
) -> datetime.datetime:
    moved = fire_at + shift
    if due_at is not None and moved > due_at > fire_at:
        return fire_at
    return moved


def item_candidates(
    item: ItemFacts, now: datetime.datetime, cal: Calendar, learning: Learning
) -> list[Candidate]:
    """``deadline``, ``overdue``, ``commitment`` and ``waiting_for`` candidates of one work item."""
    if item.lifecycle_status not in OPEN or item.archived or item.verification_status == "rejected":
        return []
    key_parts = (item.due_at, item.lifecycle_status, item.last_activity_at)
    eligible_item = not (item.verification_status == "suggested" and item.confidence_band == "low")
    out: list[Candidate] = []

    def add(kind: str, slot: str, fire_at: datetime.datetime, reason: dict[str, Any]) -> None:
        out.append(
            Candidate(
                item_type="work_item",
                item_id=item.id,
                reminder_type=kind,
                slot=slot,
                fire_at=_later(fire_at, learning.shift(kind), item.due_at),
                material_key=material_key("work_item", item.id, kind, *key_parts),
                priority=item.priority,
                proactive_eligible=eligible_item and not learning.suppressed(item.person_id, kind),
                person_id=item.person_id,
                reason={"rule": kind, "slot": slot, **reason},
            )
        )

    due = item.due_at
    if due is not None and due > now and item.priority >= learning.deadline_threshold(item.person_id):
        due_day = cal.local(due).date()
        add(
            "deadline",
            "day_before",
            cal.morning(due_day - datetime.timedelta(days=1)),
            {"threshold": learning.deadline_threshold(item.person_id)},
        )
        hard = item.due_kind in HARD_DUE_KINDS and item.due_precision in HARD_PRECISIONS
        if hard and item.direction != "my_commitment":  # the commitment rule covers the due day
            add("deadline", "due_day", min(cal.morning(due_day), due), {"hard": True})
    if due is not None and due <= now and item.direction in MINE:
        first = cal.next_work_morning(cal.local(due).date())
        cycle = 0
        if now >= first:
            cycle = cal.working_days_between(first, now) // OVERDUE_EVERY_WORKING_DAYS
        fire = cal.add_working_days(first, cycle * OVERDUE_EVERY_WORKING_DAYS)
        add("overdue", f"cycle:{cycle}", fire, {"cycle": cycle})
    if due is not None and due > now and item.direction == "my_commitment":
        today = cal.local(now).date()
        due_day = cal.local(due).date()
        if due_day in (today, today + datetime.timedelta(days=1)):
            add("commitment", "once", min(cal.morning(due_day), due), {})
    if item.direction in THEIRS:
        activity = item.last_activity_at or item.created_at
        past_due_silent = due is not None and due <= now and (activity is None or activity <= due)
        quiet = activity is not None and cal.working_days_between(activity, now) >= WAITING_QUIET_WORKING_DAYS
        if past_due_silent or quiet:
            add("waiting_for", "once", cal.next_work_slot(now), {"past_due": past_due_silent, "quiet": quiet})
    return out


@dataclass(frozen=True)
class ThreadFacts:
    id: UUID
    last_message_at: datetime.datetime | None
    last_inbound_at: datetime.datetime | None
    priority: float
    person_id: UUID | None


def follow_up_candidate(
    thread: ThreadFacts, now: datetime.datetime, cal: Calendar, learning: Learning
) -> Candidate | None:
    """The user's question or request without a reply for 3 working days (the caller selected the
    thread with ``communication.waiting_on_others``)."""
    if thread.last_message_at is None:
        return None
    if cal.working_days_between(thread.last_message_at, now) < FOLLOW_UP_WORKING_DAYS:
        return None
    return Candidate(
        item_type="conversation",
        item_id=thread.id,
        reminder_type="follow_up",
        slot="once",
        fire_at=cal.next_work_slot(now) + learning.shift("follow_up"),
        material_key=material_key(
            "conversation", thread.id, "follow_up", thread.last_message_at, thread.last_inbound_at
        ),
        priority=thread.priority,
        proactive_eligible=not learning.suppressed(thread.person_id, "follow_up"),
        person_id=thread.person_id,
        reason={"rule": "follow_up", "slot": "once", "since": thread.last_message_at.isoformat()},
    )


@dataclass(frozen=True)
class MeetingFacts:
    id: UUID
    starts_at: datetime.datetime
    status: str
    open_items: int
    priority: float
    open_questions: int = 0  # Phase 4: unresolved questions of prior related meetings and threads


def meeting_candidate(meeting: MeetingFacts, now: datetime.datetime) -> Candidate | None:
    if meeting.status == "cancelled" or (meeting.open_items < 1 and meeting.open_questions < 1):
        return None
    if not (now < meeting.starts_at <= now + MEETING_HORIZON):
        return None
    return Candidate(
        item_type="meeting",
        item_id=meeting.id,
        reminder_type="meeting_prep",
        slot="once",
        fire_at=meeting.starts_at - MEETING_PREP_LEAD,
        material_key=material_key("meeting", meeting.id, "meeting_prep", meeting.starts_at),
        priority=meeting.priority,
        proactive_eligible=True,
        person_id=None,
        reason={
            "rule": "meeting_prep",
            "slot": "once",
            "open_items": meeting.open_items,
            "open_questions": meeting.open_questions,
        },
    )


SNOOZE_PRESETS = {"1h": datetime.timedelta(hours=1), "3h": datetime.timedelta(hours=3)}
SNOOZE_MAX = datetime.timedelta(days=7)


def snooze_until(
    now: datetime.datetime, cal: Calendar, *, preset: str | None, until: datetime.datetime | None
) -> datetime.datetime | None:
    """The snooze end, or None when the request is invalid (§15.4: ≤ 7 days, after now)."""
    if preset == "tomorrow":
        target = cal.morning(cal.local(now).date() + datetime.timedelta(days=1))
    elif preset in SNOOZE_PRESETS:
        target = now + SNOOZE_PRESETS[preset]
    elif until is not None and until.tzinfo is not None:
        target = until
    else:
        return None
    return target if now < target <= now + SNOOZE_MAX else None
