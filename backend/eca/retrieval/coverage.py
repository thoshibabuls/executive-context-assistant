"""Coverage block (CONTEXT_ARCHITECTURE.md §9.4, §9.10, A2): what was searched and how fresh it is.

Absence claims must cite it. Deterministic: the same states, window and time render the same
text. A source is stale when its last successful sync is older than 1 h during the user's work
hours (Monday-Friday 09:00-18:00 local unless ``users.work_hours`` says otherwise) or older than
24 h at any time.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from eca.connections import SyncState
from eca.retrieval.temporal import TimeWindow

STALE_IN_WORK_HOURS = datetime.timedelta(hours=1)
STALE_ALWAYS = datetime.timedelta(hours=24)
DEFAULT_WORK_DAYS = (0, 1, 2, 3, 4)
DEFAULT_WORK_START = 9
DEFAULT_WORK_END = 18
GAP_STATUSES = {
    "needs_reauth": "needs you to reconnect",
    "error": "is failing to sync",
    "paused": "is paused",
    "revoked": "is disconnected (data kept)",
}


@dataclass(frozen=True)
class SourceCoverage:
    capability: str  # mail | calendar
    connected: bool
    last_success_at: datetime.datetime | None
    stale: bool
    gaps: tuple[str, ...]


@dataclass(frozen=True)
class Coverage:
    sources: tuple[SourceCoverage, ...]
    window: TimeWindow | None
    text: str
    excluded_calendar_connections: tuple[UUID, ...]
    tz: ZoneInfo

    @property
    def has_gaps(self) -> bool:
        return any(s.gaps for s in self.sources)

    def summary(self) -> dict[str, Any]:
        """Content-free form for ``retrieval_traces.coverage``."""
        return {
            "sources": [
                {
                    "capability": s.capability,
                    "connected": s.connected,
                    "last_success_at": s.last_success_at.isoformat() if s.last_success_at else None,
                    "stale": s.stale,
                    "gaps": list(s.gaps),
                }
                for s in self.sources
            ],
            "window": None
            if self.window is None
            else {"start": self.window.start.isoformat(), "end": self.window.end.isoformat()},
        }


def in_work_hours(now: datetime.datetime, tz: ZoneInfo, work_hours: dict[str, Any]) -> bool:
    local = now.astimezone(tz)
    days = tuple(work_hours.get("days", DEFAULT_WORK_DAYS))
    start = int(work_hours.get("start_hour", DEFAULT_WORK_START))
    end = int(work_hours.get("end_hour", DEFAULT_WORK_END))
    return local.weekday() in days and start <= local.hour < end


def is_stale(
    last: datetime.datetime | None, now: datetime.datetime, tz: ZoneInfo, work_hours: dict[str, Any]
) -> bool:
    if last is None:
        return True
    age = now - last
    return age > STALE_ALWAYS or (in_work_hours(now, tz, work_hours) and age > STALE_IN_WORK_HOURS)


def _fmt(moment: datetime.datetime | None, tz: ZoneInfo) -> str:
    if moment is None:
        return "never"
    local = moment.astimezone(tz)
    return (
        f"{local.strftime('%a')} {local.day} {local.strftime('%b %Y %H:%M')} {local.tzname() or ''}".rstrip()
    )


def build_coverage(
    states: Sequence[SyncState],
    *,
    now: datetime.datetime,
    tz: ZoneInfo,
    work_hours: dict[str, Any],
    window: TimeWindow | None,
) -> Coverage:
    sources: list[SourceCoverage] = []
    labels = {"mail": "email", "calendar": "calendar"}
    for capability in ("mail", "calendar"):
        holders = [s for s in states if capability in s.capabilities]
        if not holders:
            missing = "not connected" if capability == "mail" or not states else "calendar access not granted"
            sources.append(SourceCoverage(capability, False, None, False, (missing,)))
            continue
        lasts = [s.last_success.get(capability) for s in holders]
        known = [t for t in lasts if t is not None]
        last = max(known) if known else None
        gaps_list = [
            f"{s.account_email} {GAP_STATUSES[s.status]}" for s in holders if s.status in GAP_STATUSES
        ]
        stale = is_stale(last, now, tz, work_hours) and any(s.status == "active" for s in holders)
        if stale:
            gaps_list.append(
                f"{labels[capability]} has not synced since {_fmt(last, tz)}; newer updates may be missing"
            )
        sources.append(SourceCoverage(capability, True, last, stale, tuple(gaps_list)))
    excluded = tuple(
        sorted(s.connection_id for s in states if s.provider == "google" and "calendar" not in s.capabilities)
    )
    lines = []
    searched = []
    for src in sources:
        if src.connected:
            searched.append(f"{labels[src.capability]} (synced {_fmt(src.last_success_at, tz)})")
    lines.append("searched: " + (", ".join(searched) if searched else "no connected source"))
    if window is not None:
        lines.append(f"window: {_fmt(window.start, tz)} → {_fmt(window.end, tz)} ({window.label})")
    gaps = [g for src in sources for g in src.gaps]
    lines.append("gaps: " + ("; ".join(gaps) if gaps else "none"))
    return Coverage(tuple(sources), window, "\n".join(lines), excluded, tz)


def coverage_sentence(coverage: Coverage) -> str:
    """The sentence appended to absence claims and the abstention template (§9.4)."""
    searched = [
        f"{'email' if s.capability == 'mail' else 'calendar'} last synced "
        + _fmt(s.last_success_at, coverage.tz)
        for s in coverage.sources
        if s.connected
    ]
    gaps = [g for s in coverage.sources for g in s.gaps]
    text = "Searched " + (", ".join(searched) if searched else "no connected source") + "."
    if gaps:
        text += " Gaps: " + "; ".join(gaps) + "."
    return text
