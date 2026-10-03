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
from eca.retrieval.registry import all_retrievers
from eca.retrieval.retrievers import Ctx, Retriever
from eca.retrieval.search import SearchFilters
from eca.retrieval.temporal import TimeWindow
from eca.retrieval.traces import record_trace

TURN_TOKENS = 300
SESSION_TOKENS = 1500
PRIOR_MEETINGS = 2
LAST_MEETING_LOOKBACK = datetime.timedelta(days=90)


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
    if not plan.time_expression or plan.time_expression == temporal.SINCE_LAST_MEETING:
        return None  # since_last_meeting is resolved against meetings in ``context_for``
    if plan.since:
        return temporal.since(plan.time_expression, now, tz_name)
    return temporal.resolve(plan.time_expression, now, tz_name)


async def session_sources(
    uow: UnitOfWork, scope: SessionScope, now: datetime.datetime
) -> tuple[UUID, ...] | None:
    """Meeting scope (§9.7, §9.11): the meeting's own sources (calendar event and recording) plus
    those of up to 2 prior related meetings (series, participant overlap, title similarity)."""
    if scope.kind != "meeting" or scope.meeting_id is None:
        return None
    m = await meetings.meeting_record(uow, scope.meeting_id)
    if m is None:
        return ()
    prior = await meetings.prior_meetings(uow, m.id, limit=PRIOR_MEETINGS)
    ids = {m.source_item_id, *(o.source_item_id for o in prior)}
    for meeting_id in [m.id, *(o.id for o in prior)]:
        rec = await meetings.recording_for_meeting(uow, meeting_id)
        if rec is not None and rec.source_item_id is not None:
            ids.add(rec.source_item_id)
    return tuple(sorted(ids))


async def last_meeting_window(
    uow: UnitOfWork, plan: Plan, now: datetime.datetime, tz: ZoneInfo
) -> TimeWindow | None:
    """``since_last_meeting`` (§9.11): from the end of the most recent meeting that ended before
    now (not cancelled), with the named person when one is given (the first match)."""
    person_ids: list[UUID] = []
    for name in plan.person_names[:1]:
        matches = await people.match_names(uow, name, limit=1)
        if matches:
            person_ids.append(matches[0].person.id)
    past = await meetings.meeting_details_between(
        uow, now - LAST_MEETING_LOOKBACK, now, person_ids=person_ids or None, limit=200
    )
    held = [m for m in past if m.ends_at <= now and m.status != "cancelled"]
    if not held:
        return None
    last = max(held, key=lambda m: (m.ends_at, str(m.id)))
    label = f"since {last.title or 'the last meeting'} ({fmt_date(last.ends_at, tz, with_time=True)})"
    return TimeWindow(last.ends_at, now, label, "meeting")


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


async def context_for(
    uow: UnitOfWork,
    *,
    plan: Plan,
    session: SessionState,
    now: datetime.datetime,
    query_vector: str | None = None,
    embedding_model: str | None = None,
) -> tuple[Ctx, list[connections.SyncState], identity.UserSettings]:
    """The retriever context: user, timezone, scope filters (§9.7) and the plan's window."""
    settings = await identity.get_user_settings(uow)
    tz = temporal.zone(settings.timezone)
    self_p = await people.get_self_person(uow)
    states = await connections.sync_states(uow)
    window = plan_window(plan, now, settings.timezone)
    if plan.time_expression == temporal.SINCE_LAST_MEETING:
        window = await last_meeting_window(uow, plan, now, tz)
        if window is None:
            window = temporal.since("recently", now, settings.timezone)
            if window is not None:
                window = TimeWindow(
                    window.start, window.end, window.label, window.basis, "No earlier meeting was found."
                )
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
    ctx.persons[self_p.id] = self_p
    if window is not None and window.note:
        ctx.notes.append(window.note)
    return ctx, states, settings


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
    ctx, states, settings = await context_for(
        uow, plan=plan, session=session, now=now, query_vector=query_vector, embedding_model=embedding_model
    )
    tz, window = ctx.tz, ctx.window
    self_p = ctx.persons[ctx.self_id]
    table = retrievers or all_retrievers()
    retriever = table.get(plan.intent, table["unsupported"])
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
