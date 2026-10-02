"""Read side of ``communication`` for retrieval, the change feed and the day view (Phase 2).

Every statement has an explicit ``user_id`` predicate besides RLS (CONTEXT_ARCHITECTURE.md §9.7).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select

from eca.communication.models import conversations_table, message_participants_table, messages_table
from eca.communication.queries import ConversationSummary, _latest_inbound, _summary
from eca.communication.service import _REQUEST_PATTERN
from eca.platform.uow import UnitOfWork


@dataclass(frozen=True)
class LatestMessage:
    conversation_id: UUID
    message_id: UUID
    source_item_id: UUID
    sender_person_id: UUID | None
    sent_at: datetime.datetime


async def latest_messages(
    uow: UnitOfWork, conversation_ids: list[UUID], *, direction: str | None = "inbound"
) -> dict[UUID, LatestMessage]:
    """The newest live message per conversation (optionally of one direction)."""
    if not conversation_ids:
        return {}
    m = messages_table
    stmt = select(m.c.conversation_id, m.c.id, m.c.source_item_id, m.c.sender_person_id, m.c.sent_at).where(
        m.c.user_id == uow.user_id, m.c.conversation_id.in_(conversation_ids), m.c.deleted_at.is_(None)
    )
    if direction is not None:
        stmt = stmt.where(m.c.direction == direction)
    rows = await uow.session.execute(
        stmt.distinct(m.c.conversation_id).order_by(m.c.conversation_id, m.c.sent_at.desc(), m.c.id.desc())
    )
    return {
        r.conversation_id: LatestMessage(
            r.conversation_id, r.id, r.source_item_id, r.sender_person_id, r.sent_at
        )
        for r in rows
    }


async def waiting_on_others(
    uow: UnitOfWork, *, quiet_since: datetime.datetime, limit: int = 50
) -> list[ConversationSummary]:
    """Implicit waiting-for (CONTEXT_ARCHITECTURE.md §10.7 step 2): the user wrote last, with a
    question or a request, before ``quiet_since``, and nobody has answered."""
    c, m = conversations_table, messages_table
    rows = (
        await uow.session.execute(
            select(c)
            .where(
                c.c.user_id == uow.user_id,
                c.c.awaiting == "other",
                c.c.handled_by_user_at.is_(None),
                c.c.deleted_at.is_(None),
                c.c.last_outbound_at < quiet_since,
            )
            .order_by(c.c.last_outbound_at.desc(), c.c.id)
            .limit(limit * 2)
        )
    ).all()
    if not rows:
        return []
    last_out = {
        r.conversation_id: r
        for r in await uow.session.execute(
            select(m.c.conversation_id, m.c.triage, m.c.body_clean)
            .where(
                m.c.user_id == uow.user_id,
                m.c.conversation_id.in_([r.id for r in rows]),
                m.c.direction == "outbound",
                m.c.deleted_at.is_(None),
            )
            .distinct(m.c.conversation_id)
            .order_by(m.c.conversation_id, m.c.sent_at.desc(), m.c.id.desc())
        )
    }
    keep = []
    for r in rows:
        out = last_out.get(r.id)
        if out is None:
            continue
        request_type = (out.triage or {}).get("request_type")
        if request_type not in (None, "none") or _REQUEST_PATTERN.search(out.body_clean or ""):
            keep.append(r)
    keep = keep[:limit]
    latest = await _latest_inbound(uow, [r.id for r in keep])
    return [_summary(r, latest.get(r.id)) for r in keep]


async def conversations_with_people(
    uow: UnitOfWork, person_ids: list[UUID], *, since: datetime.datetime, limit: int = 20
) -> list[ConversationSummary]:
    """Threads with a message from or to any of these persons since ``since``, newest first."""
    if not person_ids:
        return []
    c, m, mp = conversations_table, messages_table, message_participants_table
    conv_ids = (
        select(m.c.conversation_id)
        .join(mp, mp.c.message_id == m.c.id)
        .where(
            m.c.user_id == uow.user_id,
            mp.c.person_id.in_(person_ids),
            m.c.sent_at >= since,
            m.c.deleted_at.is_(None),
        )
    )
    rows = (
        await uow.session.execute(
            select(c)
            .where(c.c.user_id == uow.user_id, c.c.id.in_(conv_ids), c.c.deleted_at.is_(None))
            .order_by(c.c.last_message_at.desc(), c.c.id)
            .limit(limit)
        )
    ).all()
    latest = await _latest_inbound(uow, [r.id for r in rows])
    return [_summary(r, latest.get(r.id)) for r in rows]


async def conversations_active_between(
    uow: UnitOfWork, start: datetime.datetime, end: datetime.datetime, *, limit: int = 50
) -> list[ConversationSummary]:
    """Threads with a live message sent in [start, end) (day view), by priority then recency."""
    c, m = conversations_table, messages_table
    conv_ids = select(m.c.conversation_id).where(
        m.c.user_id == uow.user_id, m.c.sent_at >= start, m.c.sent_at < end, m.c.deleted_at.is_(None)
    )
    score = func.coalesce(c.c.priority_score, -1.0)
    rows = (
        await uow.session.execute(
            select(c)
            .where(c.c.user_id == uow.user_id, c.c.id.in_(conv_ids), c.c.deleted_at.is_(None))
            .order_by(score.desc(), c.c.last_message_at.desc(), c.c.id)
            .limit(limit)
        )
    ).all()
    latest = await _latest_inbound(uow, [r.id for r in rows])
    return [_summary(r, latest.get(r.id)) for r in rows]


async def awaiting_user_since(
    uow: UnitOfWork, since: datetime.datetime, *, limit: int = 50
) -> list[ConversationSummary]:
    """Threads now awaiting a reply from the user whose latest inbound message was stored after
    ``since`` (new awaiting replies in the change feed; ``created_at`` is when it was learned)."""
    c, m = conversations_table, messages_table
    fresh = select(m.c.conversation_id).where(
        m.c.user_id == uow.user_id,
        m.c.direction == "inbound",
        m.c.created_at > since,
        m.c.deleted_at.is_(None),
    )
    rows = (
        await uow.session.execute(
            select(c)
            .where(
                c.c.user_id == uow.user_id,
                c.c.awaiting == "user",
                c.c.needs_reply,
                c.c.handled_by_user_at.is_(None),
                c.c.deleted_at.is_(None),
                c.c.id.in_(fresh),
            )
            .order_by(func.coalesce(c.c.priority_score, -1.0).desc(), c.c.last_inbound_at.desc(), c.c.id)
            .limit(limit)
        )
    ).all()
    latest = await _latest_inbound(uow, [r.id for r in rows])
    return [_summary(r, latest.get(r.id)) for r in rows]


async def conversation_sources(uow: UnitOfWork, conversation_ids: list[UUID]) -> dict[UUID, list[UUID]]:
    """Live message source items per conversation, oldest first (thread chains)."""
    if not conversation_ids:
        return {}
    m = messages_table
    out: dict[UUID, list[UUID]] = {cid: [] for cid in conversation_ids}
    for r in await uow.session.execute(
        select(m.c.conversation_id, m.c.source_item_id)
        .where(
            m.c.user_id == uow.user_id, m.c.conversation_id.in_(conversation_ids), m.c.deleted_at.is_(None)
        )
        .order_by(m.c.conversation_id, m.c.sent_at, m.c.id)
    ):
        out[r.conversation_id].append(r.source_item_id)
    return out


async def conversation_ids_of_sources(uow: UnitOfWork, source_item_ids: list[UUID]) -> dict[UUID, UUID]:
    """source item → conversation (pivot from evidence and chunks to threads)."""
    if not source_item_ids:
        return {}
    m = messages_table
    rows = await uow.session.execute(
        select(m.c.source_item_id, m.c.conversation_id).where(
            m.c.user_id == uow.user_id, m.c.source_item_id.in_(source_item_ids)
        )
    )
    return {r.source_item_id: r.conversation_id for r in rows}


async def sender_of_sources(uow: UnitOfWork, source_item_ids: list[UUID]) -> dict[UUID, UUID | None]:
    """source item → sender person (labels of evidence quotes: "email from John")."""
    if not source_item_ids:
        return {}
    m = messages_table
    rows = await uow.session.execute(
        select(m.c.source_item_id, m.c.sender_person_id).where(
            m.c.user_id == uow.user_id, m.c.source_item_id.in_(source_item_ids)
        )
    )
    return {r.source_item_id: r.sender_person_id for r in rows}
