"""Temporal semantics (CONTEXT_ARCHITECTURE.md §7.1). Pure functions of (expression, now, tz).

Windows are half-open ``[start, end)`` in UTC. "Yesterday" asked before 04:00 local is flagged
ambiguous: the answer states the window it used.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "fourteen": 14,
}
EARLY_MORNING_HOUR = 4
CHECKPOINT_MAX_AGE = datetime.timedelta(days=14)
CHECKPOINT_FALLBACK = datetime.timedelta(days=7)


@dataclass(frozen=True)
class TimeWindow:
    start: datetime.datetime
    end: datetime.datetime
    label: str  # e.g. "yesterday (Wed 30 Sep 2026)"
    basis: str  # explicit | checkpoint | default | rolling
    note: str | None = None
    ambiguous: bool = False


def zone(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except (KeyError, ValueError):
        return ZoneInfo("UTC")


def local_day(day: datetime.date, tz: ZoneInfo) -> tuple[datetime.datetime, datetime.datetime]:
    start = datetime.datetime.combine(day, datetime.time(0), tz)
    end = datetime.datetime.combine(day + datetime.timedelta(days=1), datetime.time(0), tz)
    return start.astimezone(datetime.UTC), end.astimezone(datetime.UTC)


def day_window(day: datetime.date, tz_name: str, *, label: str | None = None) -> TimeWindow:
    start, end = local_day(day, zone(tz_name))
    return TimeWindow(start, end, label or _day_label(day), "explicit")


def _day_label(day: datetime.date) -> str:
    return f"{day.strftime('%a')} {day.day} {day.strftime('%b %Y')}"


def _week_start(day: datetime.date) -> datetime.date:
    return day - datetime.timedelta(days=day.weekday())


def _number(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    return _NUMBER_WORDS.get(token)


def resolve(expression: str | None, now: datetime.datetime, tz_name: str) -> TimeWindow | None:
    """Resolve a time expression from a question; None when the question has none."""
    if not expression:
        return None
    text = expression.lower().strip()
    tz = zone(tz_name)
    local_now = now.astimezone(tz)
    today = local_now.date()
    if text == "today":
        return day_window(today, tz_name, label=f"today ({_day_label(today)})")
    if text == "yesterday":
        day = today - datetime.timedelta(days=1)
        early = local_now.hour < EARLY_MORNING_HOUR
        note = (
            f"It is before {EARLY_MORNING_HOUR:02d}:00, so 'yesterday' was taken as {_day_label(day)}."
            if early
            else None
        )
        w = day_window(day, tz_name, label=f"yesterday ({_day_label(day)})")
        return TimeWindow(w.start, w.end, w.label, "explicit", note, early)
    if text == "this_week" or text == "this week":
        start_day = _week_start(today)
        start, _ = local_day(start_day, tz)
        _, end = local_day(start_day + datetime.timedelta(days=6), tz)
        return TimeWindow(start, end, f"this week (from {_day_label(start_day)})", "explicit")
    if text == "last_week" or text == "last week":
        start_day = _week_start(today) - datetime.timedelta(days=7)
        start, _ = local_day(start_day, tz)
        _, end = local_day(start_day + datetime.timedelta(days=6), tz)
        return TimeWindow(start, end, f"last week (from {_day_label(start_day)})", "explicit")
    if text in ("recently", "recent"):
        return TimeWindow(now - datetime.timedelta(days=7), now, "the last 7 days", "rolling")
    m = re.fullmatch(r"(?:in the |over the )?(?:last|past) (\w+) (day|days|week|weeks)", text)
    if m and (n := _number(m.group(1))) is not None:
        days = n * (7 if m.group(2).startswith("week") else 1)
        return TimeWindow(now - datetime.timedelta(days=days), now, f"the last {days} days", "rolling")
    if text in WEEKDAYS:
        delta = (local_now.weekday() - WEEKDAYS.index(text)) % 7 or 7
        day = today - datetime.timedelta(days=delta)
        return day_window(day, tz_name, label=f"{text.capitalize()} ({_day_label(day)})")
    try:
        day = datetime.date.fromisoformat(text)
    except ValueError:
        return None
    return day_window(day, tz_name)


def since(expression: str, now: datetime.datetime, tz_name: str) -> TimeWindow | None:
    """ "since Monday", "since yesterday", "since 2026-09-28": from the start of that day to now."""
    window = resolve(expression, now, tz_name)
    if window is None:
        return None
    return TimeWindow(window.start, now, f"since {window.label}", "explicit", window.note, window.ambiguous)


def from_checkpoint(last_seen_at: datetime.datetime | None, now: datetime.datetime) -> TimeWindow:
    """ "What changed" without a time: the checkpoint, or the last 7 days when none or too old."""
    if last_seen_at is None:
        return TimeWindow(
            now - CHECKPOINT_FALLBACK, now, "the last 7 days", "default", "No earlier visit was recorded."
        )
    if now - last_seen_at > CHECKPOINT_MAX_AGE:
        return TimeWindow(
            now - CHECKPOINT_FALLBACK,
            now,
            "the last 7 days",
            "default",
            "Your last visit was more than 14 days ago; showing the last 7 days.",
        )
    return TimeWindow(last_seen_at, now, "since your last visit", "checkpoint")


def extract_expression(question: str) -> tuple[str | None, bool]:
    """(time expression, is_since) found in a question by rules; ``(None, False)`` when none."""
    q = question.lower()
    m = re.search(r"\bsince (yesterday|today|last week|" + "|".join(WEEKDAYS) + r"|\d{4}-\d{2}-\d{2})\b", q)
    if m:
        return m.group(1), True
    m = re.search(r"\b(?:away|out|gone|off) (?:for )?(\w+) days?\b", q)
    if m and _number(m.group(1)) is not None:
        return f"last {m.group(1)} days", True
    m = re.search(r"\b(?:in the |over the )?(?:last|past) (\w+) (?:days?|weeks?)\b", q)
    if m and _number(m.group(1)) is not None:
        return m.group(0).strip(), False
    for phrase, expr in (
        ("yesterday", "yesterday"),
        ("today", "today"),
        ("this week", "this_week"),
        ("last week", "last_week"),
        ("recently", "recently"),
    ):
        if re.search(rf"\b{phrase}\b", q):
            return expr, False
    m = re.search(r"\bon (" + "|".join(WEEKDAYS) + r"|\d{4}-\d{2}-\d{2})\b", q)
    if m:
        return m.group(1), False
    return None, False
