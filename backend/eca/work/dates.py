"""Deterministic deadline resolver (AI_PIPELINE.md §5.4, CONTEXT_ARCHITECTURE.md §7.1).

``due_at`` is set only from ``due_text`` that occurs verbatim in the evidence quote, relative to
the source's ``occurred_at`` in the user's timezone. Vague phrases ("soon") give ``due_at = None``
with precision ``fuzzy``; unparseable phrases give no deadline. A model's ``due_iso_guess`` is
never stored; it only lowers confidence when it disagrees (§5.3).
Day-precision deadlines resolve to 23:59 local time of that day.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
MONTHS = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
_MONTH_ABBR = {m[:3]: i + 1 for i, m in enumerate(MONTHS)} | {m: i + 1 for i, m in enumerate(MONTHS)}
VAGUE = (
    "soon",
    "shortly",
    "asap",
    "as soon as possible",
    "in a bit",
    "later",
    "sometime",
    "when i can",
    "eventually",
)
_ORDINAL = re.compile(r"\bthe (\d{1,2})(?:st|nd|rd|th)\b")
_MONTH_DAY = re.compile(r"\b(" + "|".join(_MONTH_ABBR) + r")\.? (\d{1,2})(?:st|nd|rd|th)?\b")
_DAY_MONTH = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)? (" + "|".join(_MONTH_ABBR) + r")\b")
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_IN_DAYS = re.compile(r"\bin (\d{1,2}) (?:days?|business days?|working days?)\b")


@dataclass(frozen=True)
class DueResolution:
    due_at: datetime.datetime | None
    precision: str | None  # datetime | day | week | fuzzy
    due_text: str | None
    kind: str | None = None  # by | on | week


def _end_of_day(day: datetime.date, tz: ZoneInfo) -> datetime.datetime:
    return datetime.datetime.combine(day, datetime.time(23, 59), tzinfo=tz)


def _next_weekday(today: datetime.date, weekday: int, *, allow_today: bool = False) -> datetime.date:
    delta = (weekday - today.weekday()) % 7
    if delta == 0 and not allow_today:
        delta = 7
    return today + datetime.timedelta(days=delta)


def _future_date(today: datetime.date, month: int, day: int) -> datetime.date | None:
    for year in (today.year, today.year + 1):
        try:
            candidate = datetime.date(year, month, day)
        except ValueError:
            return None
        if candidate >= today - datetime.timedelta(days=7):  # a recently passed date stays in this year
            return candidate
    return None


def _add_business_days(start: datetime.date, n: int) -> datetime.date:
    day = start
    while n > 0:
        day += datetime.timedelta(days=1)
        if day.weekday() < 5:
            n -= 1
    return day


def resolve_due(due_text: str | None, *, reference: datetime.datetime, timezone: str) -> DueResolution:
    if not due_text or not due_text.strip():
        return DueResolution(None, None, None)
    if reference.tzinfo is None:
        raise ValueError("reference must be timezone-aware")
    tz = ZoneInfo(timezone)
    local = reference.astimezone(tz)
    today = local.date()
    text = " ".join(due_text.lower().split())

    if any(re.search(rf"\b{re.escape(v)}\b", text) for v in VAGUE):
        return DueResolution(None, "fuzzy", due_text)
    if m := _ISO.search(text):
        try:
            return DueResolution(_end_of_day(datetime.date(*map(int, m.groups())), tz), "day", due_text, "by")
        except ValueError:
            return DueResolution(None, None, due_text)
    if re.search(r"\b(today|eod|end of (the )?day|tonight)\b", text):
        return DueResolution(_end_of_day(today, tz), "day", due_text, "by")
    if re.search(r"\btomorrow\b", text):
        return DueResolution(_end_of_day(today + datetime.timedelta(days=1), tz), "day", due_text, "by")
    if m := _IN_DAYS.search(text):
        n = int(m.group(1))
        day = (
            _add_business_days(today, n)
            if "business" in text or "working" in text
            else today + datetime.timedelta(days=n)
        )
        return DueResolution(_end_of_day(day, tz), "day", due_text, "by")
    if m := _MONTH_DAY.search(text):
        dated = _future_date(today, _MONTH_ABBR[m.group(1)], int(m.group(2)))
        return DueResolution(
            _end_of_day(dated, tz) if dated else None, "day" if dated else None, due_text, "by"
        )
    if m := _DAY_MONTH.search(text):
        dated = _future_date(today, _MONTH_ABBR[m.group(2)], int(m.group(1)))
        return DueResolution(
            _end_of_day(dated, tz) if dated else None, "day" if dated else None, due_text, "by"
        )
    if m := _ORDINAL.search(text):
        dom = int(m.group(1))
        month, year = today.month, today.year
        if dom < today.day:
            month, year = (1, year + 1) if month == 12 else (month + 1, year)
        try:
            return DueResolution(_end_of_day(datetime.date(year, month, dom), tz), "day", due_text, "by")
        except ValueError:
            return DueResolution(None, None, due_text)
    if re.search(r"\b(end of next week|next week)\b", text):
        friday = _next_weekday(today, 4, allow_today=True) + datetime.timedelta(days=7)
        return DueResolution(_end_of_day(friday, tz), "week", due_text, "week")
    if re.search(r"\b(end of (the|this) week|this week)\b", text):
        return DueResolution(
            _end_of_day(_next_weekday(today, 4, allow_today=True), tz), "week", due_text, "week"
        )
    for i, name in enumerate(WEEKDAYS):
        if re.search(rf"\b(next )?{name}\b", text):
            day = _next_weekday(today, i)
            if f"next {name}" in text and day - today < datetime.timedelta(days=7):
                day += datetime.timedelta(days=7)
            return DueResolution(_end_of_day(day, tz), "day", due_text, "by")
    return DueResolution(None, None, due_text)


def guess_disagrees(resolution: DueResolution, iso_guess: str | None) -> bool:
    """True when the model's diagnostic date guess differs from the code-resolved date."""
    if not iso_guess or resolution.due_at is None:
        return False
    try:
        guessed = datetime.date.fromisoformat(iso_guess[:10])
    except ValueError:
        return True
    return guessed != resolution.due_at.date()


def compatible_due(a: datetime.datetime | None, b: datetime.datetime | None) -> bool:
    """Equal, one missing, or within 3 days (TECHNICAL_DESIGN.md §13.6)."""
    if a is None or b is None:
        return True
    return abs(a - b) <= datetime.timedelta(days=3)
