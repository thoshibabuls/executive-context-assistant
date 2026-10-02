"""Packet assembly (CONTEXT_ARCHITECTURE.md §9.1): scope → anchors → discovery → expansion →
fold (already in the projections) → rank and budget → fixed layout → trace.

``assemble`` runs inside the caller's user transaction and makes no model call: the query
embedding (AI-04) is computed beforehand by :func:`embed_question`, outside any transaction, only
for intents that use discovery. The same inputs give the same packet (§2 principle 7).
"""

from __future__ import annotations

import datetime
import time
from dataclasses import dataclass
from uuid import UUID
from zoneinfo import ZoneInfo

from eca import connections, identity, intelligence, meetings, people
from eca.intelligence import AIClient
from eca.platform.uow import UnitOfWork
from eca.retrieval import temporal
from eca.retrieval.cards import delimit, fmt_date
from eca.retrieval.chunking import estimate_tokens
from eca.retrieval.coverage import Coverage, build_coverage
from eca.retrieval.packet import Packet, PacketItem, pack
from eca.retrieval.plan import DISCOVERY_INTENTS, FocusEntry, Plan, SessionScope, SessionState
from eca.retrieval.retrievers import RETRIEVERS, Ctx, Retriever
from eca.retrieval.search import SearchFilters
from eca.retrieval.temporal import TimeWindow
from eca.retrieval.traces import record_trace

TURN_TOKENS = 300
SESSION_TOKENS = 1500
PRIOR_MEETINGS = 2
PRIOR_MEETING_WINDOW = datetime.timedelta(days=60)
PRIOR_OVERLAP = 0.5


@dataclass(frozen=True)
class Assembly:
    plan: Plan
    packet: Packet
    coverage: Coverage
    window: TimeWindow | None
    matched: bool
    clarification: str | None
    unresolved: tuple[str, ...]
    focus: tuple[FocusEntry, ...]
    notes: tuple[str, ...]
    trace_id: UUID
    tz: ZoneInfo

    @property
    def related(self) -> list[PacketItem]:
        """Closest related items for the abstention template (AI_PIPELINE.md §5.7)."""
        return list(self.packet.items[:3])


def needs_discovery(plan: Plan) -> bool:
    return plan.intent in DISCOVERY_INTENTS or (plan.qualifier is not None)


async def embed_question(
    client: AIClient | None, question: str, *, user_id: UUID | None
) -> tuple[str | None, str | None]:
    """(vector literal, model) for hybrid search; (None, None) when embeddings are unavailable,
    so discovery falls back to full-text search (AI_PIPELINE.md §14)."""
    if client is None:
        return None, None
    try:
        result = await intelligence.embed_query(client, question, user_id=user_id)
    except intelligence.AIError:
        return None, None
    return intelligence.vector_literal(result.vectors[0]), result.model


def plan_window(plan: Plan, now: datetime.datetime, tz_name: str) -> TimeWindow | None:
    if not plan.time_expression:
        return None
    if plan.since:
        return temporal.since(plan.time_expression, now, tz_name)
    return temporal.resolve(plan.time_expression, now, tz_name)


async def session_sources(
    uow: UnitOfWork, scope: SessionScope, now: datetime.datetime
) -> tuple[UUID, ...] | None:
    """Meeting scope (§9.7, §9.10): the meeting's source plus up to 2 prior meetings."""
    if scope.kind != "meeting" or scope.meeting_id is None:
        return None
    found = await meetings.get_meeting_details(uow, [scope.meeting_id])
    if not found:
        return ()
    m = found[0]
    earlier = await meetings.meeting_details_between(
        uow, m.starts_at - PRIOR_MEETING_WINDOW, m.starts_at, include_cancelled=False, limit=200
    )
    attendees = set(m.attendee_ids)

    def related(o: meetings.MeetingDetail) -> bool:
        if o.id == m.id or o.ends_at > m.starts_at:
            return False
        if m.series_key and o.series_key == m.series_key:
            return True
        other = set(o.attendee_ids)
        return (
            bool(attendees and other)
            and len(attendees & other) / min(len(attendees), len(other)) >= PRIOR_OVERLAP
        )

    prior = [o for o in earlier if related(o)][-PRIOR_MEETINGS:]
    return tuple(sorted({m.source_item_id, *(o.source_item_id for o in prior)}))


def render_session(session: SessionState) -> str:
    """Last 4 turns (each cut to 300 tokens, at most 1,500) and the focus map (§13, §9.10)."""
    lines: list[str] = []
    total = 0
    for n, turn in enumerate(reversed(session.turns[-4:])):
        q = _cut(turn.question, TURN_TOKENS // 3)
        a = _cut(turn.answer, TURN_TOKENS)
        line = f"turn -{n + 1}: user {delimit(q)} / assistant {delimit(a)}"
        cost = estimate_tokens(line)
        if total + cost > SESSION_TOKENS:
            break
        lines.insert(0, line)
        total += cost
    if session.focus:
        lines.append("focus: " + "; ".join(f"{f.type} {delimit(f.label)}" for f in session.focus[:10]))
    return "\n".join(lines)


def _cut(text: str, tokens: int) -> str:
    limit = tokens * 4
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def assemble(
    uow: UnitOfWork,
    *,
    plan: Plan,
    question: str,
    session: SessionState,
    now: datetime.datetime,
    query_vector: str | None = None,
    embedding_model: str | None = None,
    surface: str = "chat",
    retrievers: dict[str, Retriever] | None = None,
) -> Assembly:
    started = time.monotonic()
    settings = await identity.get_user_settings(uow)
    tz = temporal.zone(settings.timezone)
    self_p = await people.get_self_person(uow)
    states = await connections.sync_states(uow)
    window = plan_window(plan, now, settings.timezone)
    excluded = tuple(
        sorted(s.connection_id for s in states if s.provider == "google" and "calendar" not in s.capabilities)
    )
    allowed = await session_sources(uow, session.scope, now)
    ctx = Ctx(
        uow=uow,
        plan=plan,
        session=session,
        now=now,
        tz=tz,
        self_id=self_p.id,
        filters=SearchFilters(exclude_calendar_connections=excluded, allowed_sources=allowed),
        window=window,
        query_vector=query_vector,
        embedding_model=embedding_model,
    )
    if window is not None and window.note:
        ctx.notes.append(window.note)
    retriever = (retrievers or RETRIEVERS).get(plan.intent, RETRIEVERS["unsupported"])
    got = await retriever(ctx)
    effective_window = got.window or window
    coverage = build_coverage(states, now=now, tz=tz, work_hours=settings.work_hours, window=effective_window)
    scope_text = "global" if session.scope.kind == "global" else f"meeting {session.scope.meeting_id}"
    frame_lines = [
        f"user: {delimit(self_p.display_name or 'the user')}",
        f"now: {fmt_date(now, tz, with_time=True)} ({settings.timezone})",
        f"scope: {scope_text}",
    ]
    frame_lines += [f"note: {n}" for n in ctx.notes]
    packet = pack(
        scenario=plan.scenario,
        frame="\n".join(frame_lines),
        coverage_text=coverage.text,
        question=delimit(question),
        session_text=render_session(session),
        candidates=got.items,
    )
    trace_id = await record_trace(
        uow,
        question=question,
        surface=surface,
        plan=plan.trace(),
        packet=packet,
        candidates=ctx.trace,
        coverage=coverage.summary(),
        latency_ms=int((time.monotonic() - started) * 1000),
    )
    return Assembly(
        plan=plan,
        packet=packet,
        coverage=coverage,
        window=effective_window,
        matched=got.matched,
        clarification=got.clarification,
        unresolved=tuple(got.unresolved),
        focus=tuple(ctx.focus),
        notes=tuple(ctx.notes),
        trace_id=trace_id,
        tz=tz,
    )
