"""Conversation read models and user commands for the API (slice 1.7, BACKEND_DESIGN.md §16.5).

Needs-response: conversations awaiting the user, not handled, ordered by priority (the
``ix_conv_needs_reply`` index), each with its reason (heuristic, triage or user) and the AI
triage of its latest inbound message labelled as a suggestion.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, or_, select, update

from eca.communication.models import conversations_table, messages_table
from eca.platform.errors import NotFound, PreconditionFailed, ValidationFailed
from eca.platform.feedback import record_feedback
from eca.platform.uow import UnitOfWork


@dataclass(frozen=True)
class ConversationSummary:
    id: UUID
    subject: str | None
    awaiting: str
    needs_reply: bool
    needs_reply_source: str | None
    last_message_at: datetime.datetime | None
    last_inbound_at: datetime.datetime | None
    handled_by_user_at: datetime.datetime | None
    priority_score: float | None
    priority_reasons: list[dict[str, Any]]
    priority_override: int | None
    version: int
    latest_triage: dict[str, Any] | None
    latest_snippet: str | None


@dataclass(frozen=True)
class MessageSummary:
    id: UUID
    source_item_id: UUID
    sender_person_id: UUID | None
    direction: str
    sent_at: datetime.datetime
    subject: str | None
    snippet: str | None
    body_available: bool
    is_bulk: bool
    triage: dict[str, Any] | None


@dataclass(frozen=True)
class ConversationPage:
    items: list[ConversationSummary]
    next_key: list[Any] | None


async def _latest_inbound(uow: UnitOfWork, conversation_ids: list[UUID]) -> dict[UUID, Any]:
    if not conversation_ids:
        return {}
    m = messages_table
    rows = await uow.session.execute(
        select(m.c.conversation_id, m.c.triage, m.c.snippet, m.c.is_bulk)
        .where(
            m.c.conversation_id.in_(conversation_ids), m.c.direction == "inbound", m.c.deleted_at.is_(None)
        )
        .distinct(m.c.conversation_id)
        .order_by(m.c.conversation_id, m.c.sent_at.desc(), m.c.id.desc())
    )
    return {r.conversation_id: r for r in rows}


def _summary(row: Any, latest: Any | None) -> ConversationSummary:
    return ConversationSummary(
        id=row.id,
        subject=row.subject,
        awaiting=row.awaiting,
        needs_reply=row.needs_reply,
        needs_reply_source=row.needs_reply_source,
        last_message_at=row.last_message_at,
        last_inbound_at=row.last_inbound_at,
        handled_by_user_at=row.handled_by_user_at,
        priority_score=row.priority_score,
        priority_reasons=list(row.priority_reasons or []),
        priority_override=row.priority_override,
        version=row.version,
        latest_triage=latest.triage if latest is not None else None,
        latest_snippet=latest.snippet if latest is not None else None,
    )


async def needs_response_page(uow: UnitOfWork, *, after: list[Any] | None, limit: int) -> ConversationPage:
    c = conversations_table
    score = func.coalesce(c.c.priority_score, -1.0)
    inbound = func.coalesce(c.c.last_inbound_at, datetime.datetime(1970, 1, 1, tzinfo=datetime.UTC))
    stmt = select(c).where(
        c.c.awaiting == "user", c.c.needs_reply, c.c.handled_by_user_at.is_(None), c.c.deleted_at.is_(None)
    )
    if after is not None:
        s0, t0, id0 = float(after[0]), datetime.datetime.fromisoformat(after[1]), UUID(after[2])
        stmt = stmt.where(
            or_(
                score < s0,
                and_(score == s0, inbound < t0),
                and_(score == s0, inbound == t0, c.c.id > id0),
            )
        )
    rows = (
        await uow.session.execute(stmt.order_by(score.desc(), inbound.desc(), c.c.id).limit(limit + 1))
    ).all()
    page = rows[:limit]
    latest = await _latest_inbound(uow, [r.id for r in page])
    next_key = None
    if len(rows) > limit and page:
        last = page[-1]
        next_key = [
            last.priority_score if last.priority_score is not None else -1.0,
            (last.last_inbound_at or datetime.datetime(1970, 1, 1, tzinfo=datetime.UTC)).isoformat(),
            str(last.id),
        ]
    return ConversationPage([_summary(r, latest.get(r.id)) for r in page], next_key)


async def conversations_by_ids(uow: UnitOfWork, ids: list[UUID]) -> list[ConversationSummary]:
    c = conversations_table
    rows = (await uow.session.execute(select(c).where(c.c.id.in_(ids), c.c.deleted_at.is_(None)))).all()
    latest = await _latest_inbound(uow, [r.id for r in rows])
    return [_summary(r, latest.get(r.id)) for r in rows]


async def conversation_detail(
    uow: UnitOfWork, conversation_id: UUID
) -> tuple[ConversationSummary, list[MessageSummary]]:
    c, m = conversations_table, messages_table
    row = (
        await uow.session.execute(select(c).where(c.c.id == conversation_id, c.c.deleted_at.is_(None)))
    ).one_or_none()
    if row is None:
        raise NotFound("conversation not found")
    msgs = (
        await uow.session.execute(
            select(
                m.c.id,
                m.c.source_item_id,
                m.c.sender_person_id,
                m.c.direction,
                m.c.sent_at,
                m.c.subject,
                m.c.snippet,
                m.c.body_purged_at,
                m.c.is_bulk,
                m.c.triage,
            )
            .where(m.c.conversation_id == conversation_id, m.c.deleted_at.is_(None))
            .order_by(m.c.sent_at, m.c.id)
        )
    ).all()
    latest = next((x for x in reversed(msgs) if x.direction == "inbound"), None)
    return _summary(row, latest), [
        MessageSummary(
            x.id,
            x.source_item_id,
            x.sender_person_id,
            x.direction,
            x.sent_at,
            x.subject,
            x.snippet,
            x.body_purged_at is None,
            x.is_bulk,
            x.triage,
        )
        for x in msgs
    ]


async def mark_handled(uow: UnitOfWork, conversation_id: UUID, *, at: datetime.datetime) -> None:
    c = conversations_table
    row = (
        await uow.session.execute(
            update(c)
            .where(c.c.id == conversation_id, c.c.deleted_at.is_(None))
            .values(
                handled_by_user_at=at, needs_reply=False, needs_reply_source="user", version=c.c.version + 1
            )
            .returning(c.c.id)
        )
    ).one_or_none()
    if row is None:
        raise NotFound("conversation not found")
    await record_feedback(uow, target_type="conversation", target_id=conversation_id, action="mark_handled")


async def set_priority_override(
    uow: UnitOfWork, conversation_id: UUID, override: int | None, *, if_match: int | None
) -> ConversationSummary:
    """User priority override: -1 (low), 0 (clear rank boost), 1 (high); ``None`` clears it."""
    if override is not None and override not in (-1, 0, 1):
        raise ValidationFailed("priority_override must be -1, 0, 1 or null")
    c = conversations_table
    cur = (
        await uow.session.execute(
            select(c.c.version, c.c.priority_override)
            .where(c.c.id == conversation_id, c.c.deleted_at.is_(None))
            .with_for_update()
        )
    ).one_or_none()
    if cur is None:
        raise NotFound("conversation not found")
    if if_match is not None and if_match != cur.version:
        raise PreconditionFailed("the conversation changed", details={"current_version": cur.version})
    await uow.session.execute(
        update(c).where(c.c.id == conversation_id).values(priority_override=override, version=c.c.version + 1)
    )
    await record_feedback(
        uow,
        target_type="conversation",
        target_id=conversation_id,
        action="priority_override",
        before={"priority_override": cur.priority_override},
        after={"priority_override": override},
    )
    return (await conversations_by_ids(uow, [conversation_id]))[0]


async def set_conversation_priority(
    uow: UnitOfWork, conversation_id: UUID, *, score: float, reasons: list[dict[str, Any]], version: int
) -> bool:
    """``attention`` writes the computed score through this service, only if the row is unchanged
    since it was read (``version``); False means a newer change will trigger another compute."""
    c = conversations_table
    result = await uow.session.execute(
        update(c)
        .where(c.c.id == conversation_id, c.c.version == version)
        .values(priority_score=score, priority_reasons=reasons)
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


@dataclass(frozen=True)
class PriorityInput:
    id: UUID
    version: int
    last_inbound_at: datetime.datetime | None
    needs_reply: bool
    awaiting: str
    priority_override: int | None
    participant_ids: tuple[UUID, ...]
    triage_urgency_signals: tuple[str, ...]
    triage_business_impact: str | None
    triage_request_type: str | None
    triage_confidence: float | None
    is_bulk: bool


async def priority_inputs(uow: UnitOfWork, conversation_ids: list[UUID] | None = None) -> list[PriorityInput]:
    """Open conversations (awaiting the user) with what the priority score needs."""
    c, m = conversations_table, messages_table
    stmt = select(c).where(c.c.awaiting == "user", c.c.handled_by_user_at.is_(None), c.c.deleted_at.is_(None))
    if conversation_ids is not None:
        stmt = select(c).where(c.c.id.in_(conversation_ids), c.c.deleted_at.is_(None))
    rows = (await uow.session.execute(stmt)).all()
    latest = await _latest_inbound(uow, [r.id for r in rows])
    senders: dict[UUID, set[UUID]] = {}
    if rows:
        for r in await uow.session.execute(
            select(m.c.conversation_id, m.c.sender_person_id).where(
                m.c.conversation_id.in_([x.id for x in rows]),
                m.c.direction == "inbound",
                m.c.sender_person_id.is_not(None),
            )
        ):
            senders.setdefault(r.conversation_id, set()).add(r.sender_person_id)
    out = []
    for r in rows:
        triage = (latest[r.id].triage or {}) if r.id in latest else {}
        out.append(
            PriorityInput(
                id=r.id,
                version=r.version,
                last_inbound_at=r.last_inbound_at,
                needs_reply=r.needs_reply,
                awaiting=r.awaiting,
                priority_override=r.priority_override,
                participant_ids=tuple(sorted(senders.get(r.id, set()))),
                triage_urgency_signals=tuple(triage.get("urgency_signals") or ()),
                triage_business_impact=triage.get("business_impact"),
                triage_request_type=triage.get("request_type"),
                triage_confidence=triage.get("confidence"),
                is_bulk=bool(latest[r.id].is_bulk) if r.id in latest else False,
            )
        )
    return out


async def message_conversation_id(uow: UnitOfWork, message_id: UUID) -> UUID | None:
    m = messages_table
    value = (
        await uow.session.execute(select(m.c.conversation_id).where(m.c.id == message_id))
    ).scalar_one_or_none()
    return UUID(str(value)) if value is not None else None
