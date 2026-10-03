"""Thread summaries and gist timelines (slice 3.2; AI_PIPELINE.md §4.2 O2, §5.9; BACKEND_DESIGN.md
§15, §17.6). ``communication`` stays the single writer of ``conversations.summary_*``.

- Gist timeline: deterministic rendering of the stored AI-01 gists (``messages.triage.gist``).
- AI-03 claim: a conditional update of ``summary_pending_through`` to the newest relevant message
  ("keyed by the last message"); the result is stored only while the claim still holds.
- Relevant message = a live message with an applied AI-01 triage (prefiltered mail has none).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update

from eca.communication.models import conversations_table, messages_table
from eca.platform.errors import NotFound
from eca.platform.uow import UnitOfWork

GIST_TIMELINE_LENGTH = 8
SUMMARY_MIN_RELEVANT = 8  # automatic AI-03 (AI_PIPELINE.md §4.2)
SUMMARY_MIN_REQUESTED = 2  # the user's "Summarize"
SUMMARY_DEBOUNCE = datetime.timedelta(minutes=10)
SUMMARY_ACTIVE_WINDOW = datetime.timedelta(days=7)
SUMMARY_MESSAGES = 20


def _relevant(m: Any) -> Any:
    return (m.c.deleted_at.is_(None)) & (m.c.triage.is_not(None))


@dataclass(frozen=True)
class GistEntry:
    message_id: UUID
    source_item_id: UUID
    sent_at: datetime.datetime
    sender_person_id: UUID | None
    direction: str
    gist: str


async def gist_timeline(
    uow: UnitOfWork, conversation_id: UUID, *, limit: int = GIST_TIMELINE_LENGTH
) -> list[GistEntry]:
    """The newest ``limit`` relevant messages with their AI-01 gist, oldest first."""
    m = messages_table
    rows = (
        await uow.session.execute(
            select(m.c.id, m.c.source_item_id, m.c.sent_at, m.c.sender_person_id, m.c.direction, m.c.triage)
            .where(m.c.user_id == uow.user_id, m.c.conversation_id == conversation_id, _relevant(m))
            .order_by(m.c.sent_at.desc(), m.c.id.desc())
            .limit(limit)
        )
    ).all()
    return [
        GistEntry(
            r.id,
            r.source_item_id,
            r.sent_at,
            r.sender_person_id,
            r.direction,
            str(r.triage.get("gist") or ""),
        )
        for r in reversed(rows)
        if isinstance(r.triage, dict)
    ]


@dataclass(frozen=True)
class ThreadSummaryView:
    text: str | None
    key_points: list[dict[str, Any]]
    through_message_id: UUID | None
    model: str | None
    prompt_version: str | None
    derived_at: datetime.datetime | None
    covered_source_ids: tuple[UUID, ...]
    ai_call_ids: tuple[UUID, ...]
    pending: bool
    stale: bool  # newer relevant messages exist than the summary covers
    relevant_messages: int


async def _latest_relevant(uow: UnitOfWork, conversation_id: UUID) -> tuple[UUID | None, int]:
    m = messages_table
    latest = (
        await uow.session.execute(
            select(m.c.id)
            .where(m.c.user_id == uow.user_id, m.c.conversation_id == conversation_id, _relevant(m))
            .order_by(m.c.sent_at.desc(), m.c.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    count = (
        await uow.session.execute(
            select(func.count()).where(
                m.c.user_id == uow.user_id, m.c.conversation_id == conversation_id, _relevant(m)
            )
        )
    ).scalar_one()
    return latest, int(count)


async def thread_summary(uow: UnitOfWork, conversation_id: UUID) -> ThreadSummaryView:
    c = conversations_table
    row = (
        await uow.session.execute(
            select(c).where(c.c.user_id == uow.user_id, c.c.id == conversation_id, c.c.deleted_at.is_(None))
        )
    ).one_or_none()
    if row is None:
        raise NotFound("conversation not found")
    latest, count = await _latest_relevant(uow, conversation_id)
    through = row.summary_through_message_id
    return ThreadSummaryView(
        text=row.summary,
        key_points=list(row.summary_key_points or []),
        through_message_id=through,
        model=row.summary_model,
        prompt_version=row.summary_prompt_version,
        derived_at=row.summary_derived_at,
        covered_source_ids=tuple(row.summary_covered_source_ids or ()),
        ai_call_ids=tuple(row.summary_ai_call_ids or ()),
        pending=row.summary_pending_through is not None,
        stale=row.summary is not None and latest is not None and latest != through,
        relevant_messages=count,
    )


async def claim_summary(
    uow: UnitOfWork, conversation_id: UUID, *, requested: bool, now: datetime.datetime
) -> UUID | None:
    """Claim AI-03 for the thread's newest relevant message; returns that message's ID when the
    claim changed the row (the caller then publishes ``ThreadSummaryDue``), else None: already
    summarized, already claimed, failed for that message (automatic runs only) or too short."""
    latest, count = await _latest_relevant(uow, conversation_id)
    if latest is None or count < (SUMMARY_MIN_REQUESTED if requested else SUMMARY_MIN_RELEVANT):
        return None
    c = conversations_table
    stmt = update(c).where(
        c.c.id == conversation_id,
        c.c.user_id == uow.user_id,
        c.c.summary_through_message_id.is_distinct_from(latest),
        c.c.summary_pending_through.is_distinct_from(latest),
    )
    if not requested:
        stmt = stmt.where(c.c.summary_failed_through.is_distinct_from(latest))
    values: dict[str, Any] = {"summary_pending_through": latest}
    if requested:
        values["summary_requested_at"] = now
    claimed = (await uow.session.execute(stmt.values(**values).returning(c.c.id))).scalar_one_or_none()
    return latest if claimed is not None else None


async def summary_candidates(uow: UnitOfWork, *, now: datetime.datetime, limit: int = 20) -> list[UUID]:
    """Threads active in the last 7 days with at least 8 relevant messages whose newest relevant
    message is at least 10 minutes old (the debounce) and not yet summarized or claimed."""
    c, m = conversations_table, messages_table
    stats = (
        select(
            m.c.conversation_id,
            func.count().label("n"),
            func.max(m.c.created_at).label("newest"),
        )
        .where(m.c.user_id == uow.user_id, _relevant(m))
        .group_by(m.c.conversation_id)
        .having(func.count() >= SUMMARY_MIN_RELEVANT)
        .subquery()
    )
    rows = await uow.session.execute(
        select(c.c.id)
        .join(stats, stats.c.conversation_id == c.c.id)
        .where(
            c.c.user_id == uow.user_id,
            c.c.deleted_at.is_(None),
            c.c.last_message_at >= now - SUMMARY_ACTIVE_WINDOW,
            c.c.summary_pending_through.is_(None),
            stats.c.newest <= now - SUMMARY_DEBOUNCE,
        )
        .order_by(c.c.last_message_at.desc(), c.c.id)
        .limit(limit)
    )
    return [r.id for r in rows]


@dataclass(frozen=True)
class SummaryMessage:
    ref: str  # M1..Mn, oldest first
    message_id: UUID
    source_item_id: UUID
    sent_at: datetime.datetime
    sender_person_id: UUID | None
    direction: str
    text: str  # clean body, or the AI-01 gist when retention purged the body
    from_gist: bool = False


@dataclass
class SummaryInput:
    conversation_id: UUID
    subject: str | None
    through_message_id: UUID
    messages: list[SummaryMessage] = field(default_factory=list)


async def summary_input(
    uow: UnitOfWork, conversation_id: UUID, through_message_id: UUID
) -> SummaryInput | None:
    """The newest 20 relevant messages up to ``through_message_id`` (clean text, never an older
    summary: no summary-of-summary). None when the claim no longer holds."""
    c, m = conversations_table, messages_table
    conv = (
        await uow.session.execute(
            select(c.c.subject, c.c.summary_pending_through).where(
                c.c.user_id == uow.user_id, c.c.id == conversation_id, c.c.deleted_at.is_(None)
            )
        )
    ).one_or_none()
    if conv is None or conv.summary_pending_through != through_message_id:
        return None
    through = (
        await uow.session.execute(select(m.c.sent_at).where(m.c.id == through_message_id))
    ).scalar_one_or_none()
    if through is None:
        return None
    rows = (
        await uow.session.execute(
            select(
                m.c.id,
                m.c.source_item_id,
                m.c.sent_at,
                m.c.sender_person_id,
                m.c.direction,
                m.c.body_clean,
                m.c.triage,
            )
            .where(
                m.c.user_id == uow.user_id,
                m.c.conversation_id == conversation_id,
                _relevant(m),
                m.c.sent_at <= through,
            )
            .order_by(m.c.sent_at.desc(), m.c.id.desc())
            .limit(SUMMARY_MESSAGES)
        )
    ).all()
    out = SummaryInput(conversation_id, conv.subject, through_message_id)
    for n, r in enumerate(reversed(rows), start=1):
        gist = str((r.triage or {}).get("gist") or "")
        body = r.body_clean or ""
        out.messages.append(
            SummaryMessage(
                f"M{n}",
                r.id,
                r.source_item_id,
                r.sent_at,
                r.sender_person_id,
                r.direction,
                body or gist,
                not body,
            )
        )
    return out


async def store_thread_summary(
    uow: UnitOfWork,
    conversation_id: UUID,
    *,
    through_message_id: UUID,
    summary: str,
    key_points: list[dict[str, Any]],
    model: str | None,
    prompt_version: str,
    ai_call_ids: list[UUID],
    covered_source_ids: list[UUID],
    now: datetime.datetime,
) -> bool:
    """Store an AI-03 result while its claim holds (AI-DERIVED columns with provenance)."""
    c = conversations_table
    stored = (
        await uow.session.execute(
            update(c)
            .where(
                c.c.id == conversation_id,
                c.c.user_id == uow.user_id,
                c.c.summary_pending_through == through_message_id,
            )
            .values(
                summary=summary,
                summary_key_points=key_points,
                summary_through_message_id=through_message_id,
                summary_method="llm",
                summary_model=model,
                summary_prompt_version=prompt_version,
                summary_derived_at=now,
                summary_ai_call_ids=ai_call_ids,
                summary_covered_source_ids=sorted(set(covered_source_ids)),
                summary_pending_through=None,
                summary_failed_through=None,
            )
            .returning(c.c.id)
        )
    ).scalar_one_or_none()
    return stored is not None


async def release_summary_claim(
    uow: UnitOfWork, conversation_id: UUID, *, through_message_id: UUID, failed: bool
) -> None:
    """Drop the claim; ``failed`` records the message so the sweep does not retry it (a new
    message or the user's request does)."""
    c = conversations_table
    values: dict[str, Any] = {"summary_pending_through": None}
    if failed:
        values["summary_failed_through"] = through_message_id
    await uow.session.execute(
        update(c)
        .where(
            c.c.id == conversation_id,
            c.c.user_id == uow.user_id,
            c.c.summary_pending_through == through_message_id,
        )
        .values(**values)
    )
