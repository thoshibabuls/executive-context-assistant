"""Relationship profiles (CONTEXT_ARCHITECTURE.md §5.3, §11.1-§11.2). Deterministic; no AI call.

``compute_profile`` is a pure function of counts and dates. ``refresh_profiles`` reads the inputs
through the owning modules' read APIs and writes the result through
``people.set_relationship_profile`` (people stays the single writer; user fields are never
touched). Triggers: nightly task, ``PersonChanged`` and ``MessageNormalized`` (``tasks``).
"""

from __future__ import annotations

import datetime
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import structlog

from eca import communication, meetings, people, work
from eca.identity import list_active_user_ids
from eca.platform.uow import UnitOfWork, UnitOfWorkFactory

log = structlog.get_logger("eca.attention.profiles")

PROFILE_VERSION = 1
WINDOW = datetime.timedelta(days=30)
UPCOMING = datetime.timedelta(days=14)
HALF_LIFE_DAYS = 60.0
TOPICS = 3
SUBJECT_THREADS = 10
WEIGHTS = {"reciprocity": 0.30, "meetings": 0.20, "role": 0.15, "organization": 0.15, "open_items": 0.20}
EXECUTIVE = re.compile(
    r"\b(chief|ceo|cto|cfo|coo|cio|president|founder|vp|vice president|director|head|partner|owner)\b",
    re.IGNORECASE,
)
_PREFIX = re.compile(r"^\s*((re|fw|fwd|aw|wg)\s*:\s*)+", re.IGNORECASE)


@dataclass(frozen=True)
class ProfileInputs:
    inbound: int
    outbound: int
    meetings_past: int
    role_title: str | None
    org_importance: int | None
    open_mine: int
    open_theirs: int
    last_interaction_at: datetime.datetime | None
    last_inbound_at: datetime.datetime | None
    last_outbound_at: datetime.datetime | None
    last_meeting_at: datetime.datetime | None
    next_meeting_at: datetime.datetime | None
    hints: tuple[tuple[str, datetime.datetime], ...] = ()  # AI-derived project hints
    subjects: tuple[tuple[str, datetime.datetime], ...] = ()  # thread subjects (computed)


def reciprocity(inbound: int, outbound: int) -> float:
    if inbound and outbound:
        return 1.0
    if outbound:
        return 0.5
    if inbound:
        return 0.25
    return 0.0


def normalize_topic(text: str) -> str:
    return " ".join(_PREFIX.sub("", text).split()).strip().lower()


def active_topics(
    hints: Sequence[tuple[str, datetime.datetime]], subjects: Sequence[tuple[str, datetime.datetime]]
) -> list[dict[str, str]]:
    """Top topics by frequency, ties by recency; hints are labelled ``inferred`` (AI-derived)."""
    count: Counter[tuple[str, str]] = Counter()
    latest: dict[tuple[str, str], datetime.datetime] = {}
    shown: dict[tuple[str, str], str] = {}
    for origin, values in (("inferred", hints), ("computed", subjects)):
        for text, at in values:
            key = (normalize_topic(text), origin)
            if not key[0]:
                continue
            count[key] += 1
            latest[key] = max(latest.get(key, at), at)
            shown.setdefault(key, " ".join(_PREFIX.sub("", text).split()))
    ranked = sorted(count, key=lambda k: (-count[k], -latest[k].timestamp(), k))
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for key in ranked:
        if key[0] in seen:
            continue
        seen.add(key[0])
        out.append({"text": shown[key], "origin": key[1]})
        if len(out) == TOPICS:
            break
    return out


def _iso(value: datetime.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def compute_profile(inp: ProfileInputs, now: datetime.datetime) -> tuple[float, dict[str, Any]]:
    """(importance_inferred, profile document). Same inputs, same output."""
    terms = {
        "reciprocity": reciprocity(inp.inbound, inp.outbound),
        "meetings": min(inp.meetings_past / 4.0, 1.0),
        "role": 1.0 if inp.role_title and EXECUTIVE.search(inp.role_title) else 0.0,
        "organization": min(max((inp.org_importance or 0) / 5.0, 0.0), 1.0),
        "open_items": min((inp.open_mine + inp.open_theirs) / 3.0, 1.0),
    }
    base = sum(WEIGHTS[k] * v for k, v in terms.items())
    if inp.last_interaction_at is None:
        decay = 1.0 if base > 0 else 0.0
    else:
        days = max((now - inp.last_interaction_at).total_seconds() / 86400.0, 0.0)
        decay = 0.5 ** (days / HALF_LIFE_DAYS)
    inferred = round(base * decay, 3)
    recency = None
    if inp.last_inbound_at is not None and inp.last_outbound_at is not None:
        last = max(inp.last_inbound_at, inp.last_outbound_at)
        recency = max(int((now - last).total_seconds() // 86400), 0)
    profile: dict[str, Any] = {
        "version": PROFILE_VERSION,
        "window_days": WINDOW.days,
        "inbound_30d": inp.inbound,
        "outbound_30d": inp.outbound,
        "meetings_30d": inp.meetings_past,
        "open_mine": inp.open_mine,
        "open_theirs": inp.open_theirs,
        "interaction_recency_days": recency,
        "active_topics": active_topics(inp.hints, inp.subjects),
        "last_meeting_at": _iso(inp.last_meeting_at),
        "next_meeting_at": _iso(inp.next_meeting_at),
        "terms": {k: round(v, 3) for k, v in terms.items()},
        "decay": round(decay, 3),
        "computed_at": now.isoformat(),
    }
    return inferred, profile


async def refresh_profiles(
    uow: UnitOfWork, *, now: datetime.datetime, person_ids: list[UUID] | None = None
) -> int:
    """Recompute the profiles of the given persons (merged IDs redirected), or of every active
    person of the user. Returns the number of profiles written."""
    subjects = await people.profile_subjects(uow, now=now, person_ids=person_ids)
    if not subjects:
        return 0
    since = now - WINDOW
    all_ids = sorted({i for s in subjects for i in s.merged_ids})
    counts = await communication.interaction_counts(uow, all_ids, since=since)
    owed = await work.person_work(uow, all_ids, since=since)
    held = await meetings.meeting_details_between(uow, since, now + UPCOMING, person_ids=all_ids, limit=1000)
    written = 0
    for s in subjects:
        ids = set(s.merged_ids)
        mine = [m for m in held if ids & set(m.attendee_ids) and m.status != "cancelled"]
        past = [m for m in mine if m.starts_at <= now]
        future = [m for m in mine if m.starts_at > now]
        inbound = sum(counts[i].inbound for i in ids if i in counts)
        outbound = sum(counts[i].outbound for i in ids if i in counts)
        subjects_of: list[tuple[str, datetime.datetime]] = []
        if inbound or outbound:
            threads = await communication.conversations_with_people(
                uow, sorted(ids), since=since, limit=SUBJECT_THREADS
            )
            subjects_of = [(c.subject, c.last_message_at or now) for c in threads if c.subject]
        inp = ProfileInputs(
            inbound=inbound,
            outbound=outbound,
            meetings_past=len(past),
            role_title=s.role_title,
            org_importance=s.org_importance,
            open_mine=sum(owed[i].open_mine for i in ids if i in owed),
            open_theirs=sum(owed[i].open_theirs for i in ids if i in owed),
            last_interaction_at=s.last_interaction_at,
            last_inbound_at=s.last_inbound_at,
            last_outbound_at=s.last_outbound_at,
            last_meeting_at=max((m.starts_at for m in past), default=None),
            next_meeting_at=min((m.starts_at for m in future), default=None),
            hints=tuple(h for i in ids if i in owed for h in owed[i].hints),
            subjects=tuple(subjects_of),
        )
        inferred, profile = compute_profile(inp, now)
        await people.set_relationship_profile(
            uow, s.id, importance_inferred=inferred, profile=profile, at=now
        )
        written += 1
    return written


async def refresh_all(factory: UnitOfWorkFactory, *, now: datetime.datetime) -> int:
    async with factory(user_id=None) as uow:
        users = await list_active_user_ids(uow)
    total = 0
    for user_id in users:
        async with factory(user_id=user_id) as uow:
            total += await refresh_profiles(uow, now=now)
    log.info("relationship_profiles", users=len(users), written=total)
    return total
