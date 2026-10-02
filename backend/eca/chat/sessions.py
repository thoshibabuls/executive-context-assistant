"""Chat sessions and messages (BACKEND_DESIGN.md §16.5, §16.7; CONTEXT_ARCHITECTURE.md §13).

Session context is the last 4 turns plus the entity focus map; no session summary is generated
(AI-13 retired). The focus map resets after 2 hours of inactivity. Questions are USER-AUTHORED;
answers are AI-DERIVED rows that keep citation snapshots (text and source references), so a
history stays readable after re-apply or deletion of the cited items (BACKEND_DESIGN.md §8.5).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import delete, insert, select, tuple_, update

from eca import retrieval
from eca.chat.models import chat_messages_table, chat_sessions_table
from eca.platform.errors import NotFound, ValidationFailed
from eca.platform.feedback import record_feedback
from eca.platform.ids import uuid7
from eca.platform.uow import UnitOfWork

FOCUS_EXPIRY = datetime.timedelta(hours=2)
FOCUS_SIZE = 10
TURNS = 4
TITLE_CHARS = 80


@dataclass(frozen=True)
class SessionView:
    id: UUID
    title: str | None
    scope: dict[str, Any]
    focus: list[dict[str, Any]]
    version: int
    created_at: datetime.datetime | None
    last_active_at: datetime.datetime | None


@dataclass(frozen=True)
class MessageView:
    id: UUID
    session_id: UUID
    role: str
    content: str
    reply_to_id: UUID | None
    scenario: str | None
    answer_tier: str | None
    claims: list[dict[str, Any]]
    citations: list[dict[str, Any]]
    confidence: str | None
    provenance: dict[str, Any] | None
    retrieval_trace_id: UUID | None
    created_at: datetime.datetime | None


def _session(r: Any) -> SessionView:
    return SessionView(
        r.id,
        r.title,
        dict(r.scope or {}),
        list(r.session_entities or []),
        r.version,
        r.created_at,
        r.last_active_at,
    )


def _message(r: Any) -> MessageView:
    return MessageView(
        r.id,
        r.session_id,
        r.role,
        r.content,
        r.reply_to_id,
        r.scenario,
        r.answer_tier,
        list(r.claims or []),
        list(r.citations or []),
        r.confidence,
        dict(r.provenance) if r.provenance else None,
        r.retrieval_trace_id,
        r.created_at,
    )


def scope_of(view: SessionView) -> retrieval.SessionScope:
    if view.scope.get("kind") == "meeting" and view.scope.get("meeting_id"):
        return retrieval.SessionScope("meeting", UUID(str(view.scope["meeting_id"])))
    return retrieval.SessionScope()


async def create_session(
    uow: UnitOfWork, *, scope: dict[str, Any] | None, now: datetime.datetime
) -> SessionView:
    clean: dict[str, Any] = {"kind": "global"}
    if scope and scope.get("kind") == "meeting":
        try:
            meeting_id = UUID(str(scope.get("meeting_id")))
        except ValueError as exc:
            raise ValidationFailed("meeting scope needs a meeting_id") from exc
        allowed = await retrieval.session_sources(uow, retrieval.SessionScope("meeting", meeting_id), now)
        if not allowed:
            raise NotFound("meeting not found")
        clean = {"kind": "meeting", "meeting_id": str(meeting_id)}
    elif scope and scope.get("kind") not in (None, "global"):
        raise ValidationFailed("scope kind must be global or meeting")
    session_id = uuid7()
    await uow.session.execute(
        insert(chat_sessions_table).values(
            id=session_id,
            user_id=uow.user_id,
            scope=clean,
            session_entities=[],
            version=1,
            last_active_at=now,
        )
    )
    return await get_session(uow, session_id)


async def get_session(uow: UnitOfWork, session_id: UUID, *, for_update: bool = False) -> SessionView:
    t = chat_sessions_table
    stmt = select(t).where(t.c.id == session_id, t.c.user_id == uow.user_id)
    if for_update:
        stmt = stmt.with_for_update()
    row = (await uow.session.execute(stmt)).one_or_none()
    if row is None:
        raise NotFound("chat session not found")
    return _session(row)


async def list_sessions(
    uow: UnitOfWork, *, after: list[Any] | None, limit: int
) -> tuple[list[SessionView], list[Any] | None]:
    t = chat_sessions_table
    stmt = select(t).where(t.c.user_id == uow.user_id)
    if after is not None:
        stmt = stmt.where(
            tuple_(t.c.last_active_at, t.c.id)
            < tuple_(datetime.datetime.fromisoformat(str(after[0])), UUID(str(after[1])))
        )
    rows = (
        await uow.session.execute(stmt.order_by(t.c.last_active_at.desc(), t.c.id.desc()).limit(limit + 1))
    ).all()
    page = rows[:limit]
    next_key = [page[-1].last_active_at.isoformat(), str(page[-1].id)] if len(rows) > limit and page else None
    return [_session(r) for r in page], next_key


async def messages_of(uow: UnitOfWork, session_id: UUID, *, limit: int = 200) -> list[MessageView]:
    m = chat_messages_table
    rows = await uow.session.execute(
        select(m)
        .where(m.c.session_id == session_id, m.c.user_id == uow.user_id)
        .order_by(m.c.created_at, m.c.id)
        .limit(limit)
    )
    return [_message(r) for r in rows]


async def get_message(uow: UnitOfWork, message_id: UUID) -> MessageView:
    m = chat_messages_table
    row = (
        await uow.session.execute(select(m).where(m.c.id == message_id, m.c.user_id == uow.user_id))
    ).one_or_none()
    if row is None:
        raise NotFound("chat message not found")
    return _message(row)


async def delete_session(uow: UnitOfWork, session_id: UUID) -> None:
    """Permanent (BACKEND_DESIGN.md §13.1): messages, then the session."""
    await get_session(uow, session_id, for_update=True)
    m, t = chat_messages_table, chat_sessions_table
    await uow.session.execute(
        update(m).where(m.c.session_id == session_id, m.c.user_id == uow.user_id).values(reply_to_id=None)
    )
    await uow.session.execute(delete(m).where(m.c.session_id == session_id, m.c.user_id == uow.user_id))
    await uow.session.execute(delete(t).where(t.c.id == session_id, t.c.user_id == uow.user_id))


async def add_user_message(uow: UnitOfWork, session_id: UUID, text: str) -> UUID:
    message_id = uuid7()
    await uow.session.execute(
        insert(chat_messages_table).values(
            id=message_id,
            session_id=session_id,
            user_id=uow.user_id,
            role="user",
            content=text,
            claims=[],
            citations=[],
            ai_call_ids=[],
        )
    )
    return message_id


@dataclass(frozen=True)
class AssistantMessage:
    content: str
    reply_to_id: UUID
    scenario: str
    answer_tier: str
    claims: list[dict[str, Any]]
    citations: list[dict[str, Any]]
    confidence: str | None
    provenance: dict[str, Any]
    retrieval_trace_id: UUID | None
    ai_call_ids: list[UUID]


async def add_assistant_message(uow: UnitOfWork, session_id: UUID, msg: AssistantMessage) -> UUID:
    message_id = uuid7()
    await uow.session.execute(
        insert(chat_messages_table).values(
            id=message_id,
            session_id=session_id,
            user_id=uow.user_id,
            role="assistant",
            content=msg.content,
            reply_to_id=msg.reply_to_id,
            scenario=msg.scenario,
            answer_tier=msg.answer_tier,
            claims=msg.claims,
            citations=msg.citations,
            confidence=msg.confidence,
            provenance=msg.provenance,
            retrieval_trace_id=msg.retrieval_trace_id,
            ai_call_ids=msg.ai_call_ids,
        )
    )
    return message_id


async def load_state(
    uow: UnitOfWork, view: SessionView, *, now: datetime.datetime, before_message: UUID | None = None
) -> retrieval.SessionState:
    """Last 4 completed turns and the focus map (reset after 2 h of inactivity, §13)."""
    history = [m for m in await messages_of(uow, view.id) if m.id != before_message]
    answers = {m.reply_to_id: m for m in history if m.role == "assistant" and m.reply_to_id}
    turns = [
        retrieval.Turn(q.content, answers[q.id].content)
        for q in history
        if q.role == "user" and q.id in answers
    ][-TURNS:]
    expired = view.last_active_at is None or now - view.last_active_at > FOCUS_EXPIRY
    focus = () if expired else tuple(retrieval.FocusEntry.from_json(f) for f in view.focus[:FOCUS_SIZE])
    return retrieval.SessionState(scope_of(view), tuple(turns), focus, view.last_active_at)


def merge_focus(
    current: tuple[retrieval.FocusEntry, ...], new: list[retrieval.FocusEntry], *, turn: int
) -> list[dict[str, Any]]:
    """Newest first, one entry per entity, at most 10 (§13)."""
    merged: list[retrieval.FocusEntry] = []
    seen: set[UUID] = set()
    for entry in [retrieval.FocusEntry(e.type, e.id, e.label[:TITLE_CHARS], turn) for e in new] + list(
        current
    ):
        if entry.id in seen:
            continue
        seen.add(entry.id)
        merged.append(entry)
    return [e.as_json() for e in merged[:FOCUS_SIZE]]


async def touch_session(
    uow: UnitOfWork,
    session_id: UUID,
    *,
    focus: list[dict[str, Any]],
    title: str | None,
    now: datetime.datetime,
) -> None:
    t = chat_sessions_table
    values: dict[str, Any] = {"session_entities": focus, "last_active_at": now, "version": t.c.version + 1}
    if title is not None:
        values["title"] = title[:TITLE_CHARS]
    await uow.session.execute(
        update(t).where(t.c.id == session_id, t.c.user_id == uow.user_id).values(**values)
    )


async def record_message_feedback(
    uow: UnitOfWork, message_id: UUID, *, rating: str, reason: str | None
) -> None:
    message = await get_message(uow, message_id)
    if message.role != "assistant":
        raise ValidationFailed("feedback applies to answers")
    await record_feedback(
        uow,
        target_type="chat_message",
        target_id=message_id,
        action=rating,
        after={"reason": reason} if reason else None,
    )


async def purge_user(uow: UnitOfWork) -> None:
    m, t = chat_messages_table, chat_sessions_table
    await uow.session.execute(update(m).where(m.c.user_id == uow.user_id).values(reply_to_id=None))
    await uow.session.execute(delete(m).where(m.c.user_id == uow.user_id))
    await uow.session.execute(delete(t).where(t.c.user_id == uow.user_id))
