"""Meeting retrievers (slice 4.4, CONTEXT_ARCHITECTURE.md §9.11, §10.4, §10.5).

- ``meeting_prep`` (S4, T2 AI-07): the target meeting, the attendees' open items with quotes, the
  prior related meetings' decisions and open questions, and up to 3 chunks on the question's
  topic from attendee-linked sources of the last 30 days.
- ``cross_meeting`` (S5, T2 AI-07): up to 5 related meetings; their decisions and open questions
  with resolution chains (from any source); hybrid search on the topic restricted to their sources.
- In a meeting-scoped session: ``meeting_lookup`` (deterministic list of the meeting's items,
  decisions and open questions), ``meeting_transcript`` (T1 AI-06 over the meeting's transcript
  chunks and items) and ``meeting_synthesis`` (T2 AI-07 over the summary, concerns, decisions,
  items and the top transcript chunks).

Relational expansion first, discovery only for topics (§6.1). No model call here.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import replace
from typing import Any
from uuid import UUID

from eca import meetings, work
from eca.retrieval import cards
from eca.retrieval.cards import delimit, fmt_date
from eca.retrieval.packet import PacketItem
from eca.retrieval.plan import FocusEntry
from eca.retrieval.retrievers import Ctx, Retrieved, _item_cards, _quote_cards, resolve_people
from eca.retrieval.search import SearchFilters, hybrid_search, ts_terms
from eca.retrieval.temporal import TimeWindow

PREP_TARGET_WINDOW = datetime.timedelta(hours=24)
PREP_DISCOVERY_DAYS = 30
CROSS_LOOKBACK_DAYS = 120
CROSS_MEETINGS = 5
TOPIC_CHUNKS = 3
TRANSCRIPT_CHUNKS = 8
GROUPS = {
    "my_commitment": "What you owe",
    "my_task": "What you owe",
    "waiting_for": "What others owe you",
    "delegated": "What others owe you",
    "unresolved": "Owner unclear (confirm the speaker)",
}


def _words(text: str | None) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 3}


async def _details(ctx: Ctx, meeting_id: UUID) -> meetings.MeetingDetail | None:
    found = await meetings.get_meeting_details(ctx.uow, [meeting_id])
    return found[0] if found else None


async def _sources_of(ctx: Ctx, meeting_ids: list[UUID]) -> list[UUID]:
    """Calendar and recording source items of these meetings."""
    out: list[UUID] = []
    for d in await meetings.get_meeting_details(ctx.uow, meeting_ids):
        out.append(d.source_item_id)
        rec = await meetings.recording_for_meeting(ctx.uow, d.id)
        if rec is not None and rec.source_item_id is not None and rec.source_item_id not in out:
            out.append(rec.source_item_id)
    return out


async def _meeting_card(ctx: Ctx, m: meetings.MeetingDetail, *, priority: str = "anchor") -> PacketItem:
    await ctx.load_persons(list(m.attendee_ids))
    ctx.focus.append(FocusEntry("meeting", m.id, m.title or "meeting", 0))
    return cards.meeting_card(
        m,
        ctx.persons,
        ctx.self_id,
        ctx.tz,
        ctx.now,
        section="anchors",
        priority="anchor" if priority == "anchor" else "anchor_timeline",
    )


async def _decision_cards(ctx: Ctx, decisions: list[work.DecisionView], *, anchor: bool) -> list[PacketItem]:
    out = []
    for d in decisions:
        out.append(
            cards.decision_card(
                d,
                ctx.tz,
                section="anchors" if anchor else "state",
                priority="anchor_timeline" if anchor else "other_state",
            )
        )
        ctx.note_candidate(f"decision:{d.id}", "sql", 1.0)
    return out


async def _target_for_prep(ctx: Ctx) -> meetings.MeetingDetail | None:
    """The session's meeting; else a meeting with a named person or title words today or
    tomorrow; else the next meeting within 24 h (§9.11)."""
    meeting_id = ctx.plan.meeting_id or (
        ctx.session.scope.meeting_id if ctx.session.scope.kind == "meeting" else None
    )
    if meeting_id is not None:
        return await _details(ctx, meeting_id)
    local = ctx.now.astimezone(ctx.tz)
    end_tomorrow = (
        local.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=2)
    ).astimezone(datetime.UTC)
    who = await resolve_people(ctx)
    upcoming = await meetings.meeting_details_between(
        ctx.uow, ctx.now, end_tomorrow, person_ids=who.ids or None, limit=20
    )
    upcoming = [m for m in upcoming if m.starts_at >= ctx.now and m.status != "cancelled"]
    words = _words(ctx.plan.topic)
    if words:
        titled = [m for m in upcoming if words & _words(m.title)]
        if titled:
            return titled[0]
    if who.ids and upcoming:
        return upcoming[0]
    soon = [m for m in upcoming if m.starts_at <= ctx.now + PREP_TARGET_WINDOW]
    return soon[0] if soon else None


_OPEN = frozenset({"open", "in_progress"})
_CHANGE_WORDS = {
    "new": "new",
    "status": "status changed",
    "deadline": "deadline changed",
    "owner": "owner changed",
    "conflict": "conflicting information",
    "decided": "decided",
    "resolved": "resolved",
    "superseded": "superseded",
}


def _section_ids(sections: dict[str, Any], names: tuple[str, ...]) -> list[UUID]:
    out: list[UUID] = []
    for name in names:
        for entry in sections.get(name) or []:
            try:
                value = UUID(str(entry.get("id")))
            except (TypeError, ValueError, AttributeError):
                continue
            if value not in out:
                out.append(value)
    return out


def _change_cards(entries: list[dict[str, Any]], ctx: Ctx) -> list[PacketItem]:
    """The prep section "what changed since the previous meeting" (computed, no AI)."""
    out = []
    for n, entry in enumerate(entries[:10]):
        try:
            entity_id = UUID(str(entry.get("entity_id")))
        except (TypeError, ValueError):
            continue
        recorded = entry.get("recorded_at")
        when = fmt_date(datetime.datetime.fromisoformat(recorded), ctx.tz) if recorded else "recently"
        what = _CHANGE_WORDS.get(str(entry.get("kind")), str(entry.get("kind")))
        line = f"{when}: {what} — {delimit(entry.get('title') or 'untitled')}"
        out.append(
            PacketItem(
                key=f"change:{entity_id}:{n}",
                kind="event",
                section="timeline",
                priority="anchor_timeline",
                text=f"[changed since the previous meeting] {line}",
                line=line,
                data_class="computed",
                claim_kind="source",
                authority=1,
                entity_id=entity_id,
            )
        )
    return out


async def meeting_prep(ctx: Ctx) -> Retrieved:
    m = await _target_for_prep(ctx)
    if m is None:
        ctx.notes.append("No upcoming meeting was found in the next 24 hours.")
        return Retrieved([], False)
    items: list[PacketItem] = [await _meeting_card(ctx, m)]
    attendees = set(m.attendee_ids) - {ctx.self_id}
    stored = await meetings.get_prep(ctx.uow, m.id)
    changed: list[PacketItem] = []
    if stored is not None and stored.sections:
        # The stored prep sections (the prep view's content); each entry re-read for current state.
        sections = stored.sections
        listed = _section_ids(sections, ("open_items_mine", "open_items_theirs", "deadlines"))
        by_id = {v.id: v for v in await work.items_by_ids(ctx.uow, listed)}
        involved = [by_id[i] for i in listed if i in by_id and by_id[i].lifecycle_status in _OPEN]
        question_ids = _section_ids(sections, ("unresolved_questions",))
        open_questions = await work.decisions_by_ids(ctx.uow, question_ids)
        changed = _change_cards(sections.get("what_changed") or [], ctx)
    else:
        involved = [
            i
            for i in await work.open_items(ctx.uow)
            if {p for p in (i.owner_person_id, i.counterparty_person_id, i.requester_person_id) if p}
            & attendees
        ]
        involved.sort(
            key=lambda i: (i.due_at or datetime.datetime.max.replace(tzinfo=datetime.UTC), str(i.id))
        )
        open_questions = []
    items += await _item_cards(ctx, involved[:15], section="anchors", priority="anchor")
    items += await _quote_cards(ctx, "work_item", [i.id for i in involved[:8]], per_item=1)
    items += changed
    prior = await meetings.prior_meetings(ctx.uow, m.id, limit=2)
    decisions: list[work.DecisionView] = list(open_questions)
    seen = {d.id for d in decisions}
    for p in prior:
        mw = await work.meeting_work(ctx.uow, meeting_id=p.id, source_item_id=None)
        decisions += [d for d in mw.open_questions + mw.decisions if d.id not in seen]
        seen |= {d.id for d in decisions}
    items += await _decision_cards(ctx, decisions, anchor=True)
    if prior:
        ctx.notes.append(
            f"Previous related meeting: {prior[0].title or 'untitled'} "
            f"({fmt_date(prior[0].starts_at, ctx.tz)})."
        )
    if ctx.plan.topic and attendees:
        hits = await hybrid_search(
            ctx.uow,
            terms=ts_terms(ctx.plan.topic),
            query_vector=ctx.query_vector,
            embedding_model=ctx.embedding_model,
            filters=SearchFilters(
                since=ctx.now - datetime.timedelta(days=PREP_DISCOVERY_DAYS),
                person_ids=tuple(sorted(attendees)),
                allowed_sources=ctx.filters.allowed_sources,
                exclude_calendar_connections=ctx.filters.exclude_calendar_connections,
            ),
        )
        items += [cards.chunk_card(h, ctx.tz, score=h.rrf) for h in hits[:TOPIC_CHUNKS]]
    window = TimeWindow(ctx.now, m.starts_at, f"before {m.title or 'the meeting'}", "default")
    return Retrieved(items, bool(involved or decisions or changed), window=window)


async def _cross_anchor(ctx: Ctx) -> meetings.MeetingDetail | None:
    meeting_id = ctx.plan.meeting_id or (
        ctx.session.scope.meeting_id if ctx.session.scope.kind == "meeting" else None
    )
    if meeting_id is not None:
        return await _details(ctx, meeting_id)
    who = await resolve_people(ctx)
    past = await meetings.meeting_details_between(
        ctx.uow,
        ctx.now - datetime.timedelta(days=CROSS_LOOKBACK_DAYS),
        ctx.now,
        person_ids=who.ids or None,
        limit=200,
    )
    past = [m for m in past if m.ends_at <= ctx.now]
    words = _words(ctx.plan.topic)
    if words:
        titled = [m for m in past if words & _words(m.title)]
        if titled:
            return titled[-1]
    return past[-1] if past else None


async def cross_meeting(ctx: Ctx) -> Retrieved:
    anchor = await _cross_anchor(ctx)
    if anchor is None:
        ctx.notes.append("No earlier meeting matching the question was found.")
        return Retrieved([], False)
    prior = await meetings.prior_meetings(ctx.uow, anchor.id, limit=CROSS_MEETINGS - 1)
    set_ids = [anchor.id, *(p.id for p in prior)]
    found = await meetings.get_meeting_details(ctx.uow, set_ids)
    items: list[PacketItem] = []
    for m in found:
        items.append(await _meeting_card(ctx, m, priority="timeline"))
    decisions: dict[UUID, work.DecisionView] = {}
    for mid in set_ids:
        mw = await work.meeting_work(ctx.uow, meeting_id=mid, source_item_id=None)
        for d in mw.decisions + mw.open_questions:
            decisions[d.id] = d
    chained = [
        x
        for d in decisions.values()
        for x in (d.resolved_by_id, d.superseded_by_id)
        if x and x not in decisions
    ]
    for d in await work.decisions_by_ids(ctx.uow, chained):
        decisions[d.id] = d  # resolution chains from any source (A8)
    ordered = sorted(decisions.values(), key=lambda d: (d.decided_at or d.created_at or ctx.now, str(d.id)))
    items += await _decision_cards(ctx, ordered, anchor=True)
    sources = await _sources_of(ctx, set_ids)
    by_item, _ = await work.item_ids_for_sources(ctx.uow, sources)
    meeting_items = await work.items_by_ids(ctx.uow, sorted(by_item))
    items += await _item_cards(ctx, meeting_items[:20])
    hits = []
    if ctx.plan.topic:
        hits = await hybrid_search(
            ctx.uow,
            terms=ts_terms(ctx.plan.topic),
            query_vector=ctx.query_vector,
            embedding_model=ctx.embedding_model,
            filters=SearchFilters(
                allowed_sources=tuple(sources),
                exclude_calendar_connections=ctx.filters.exclude_calendar_connections,
            ),
        )
        items += [cards.chunk_card(h, ctx.tz, score=h.rrf) for h in hits[:6]]
    matched = bool(ordered or meeting_items or any(h.is_match for h in hits))
    return Retrieved(items, matched)


# ---------------------------------------------------------------- meeting-scoped sessions


async def _session_meeting(ctx: Ctx) -> meetings.MeetingRecord | None:
    meeting_id = ctx.plan.meeting_id or ctx.session.scope.meeting_id
    return await meetings.meeting_record(ctx.uow, meeting_id) if meeting_id else None


async def meeting_lookup(ctx: Ctx) -> Retrieved:
    """Deterministic: the meeting's items with owners and due dates, decisions, open questions."""
    m = await _session_meeting(ctx)
    if m is None:
        return Retrieved([], False)
    rec = await meetings.recording_for_meeting(ctx.uow, m.id)
    mw = await work.meeting_work(ctx.uow, meeting_id=m.id, source_item_id=rec.source_item_id if rec else None)
    items: list[PacketItem] = []
    for card, view in zip(await _item_cards(ctx, mw.items), mw.items, strict=True):
        items.append(replace(card, group=GROUPS.get(view.direction, "Other actions")))
    for card in await _decision_cards(ctx, mw.decisions, anchor=False):
        items.append(replace(card, group="Decided"))
    for card in await _decision_cards(ctx, mw.open_questions, anchor=False):
        items.append(replace(card, group="Open questions"))
    ctx.focus.append(FocusEntry("meeting", m.id, m.title or "meeting", 0))
    if rec is None or rec.status not in ("ready",):
        ctx.notes.append("This meeting's recording is not fully processed; the list may be incomplete.")
    return Retrieved(items, bool(items))


async def _transcript_hits(ctx: Ctx, m: meetings.MeetingRecord, topic: str) -> list[PacketItem]:
    rec = await meetings.recording_for_meeting(ctx.uow, m.id)
    if rec is None or rec.source_item_id is None:
        ctx.notes.append("This meeting has no transcript.")
        return []
    hits = await hybrid_search(
        ctx.uow,
        terms=ts_terms(topic),
        query_vector=ctx.query_vector,
        embedding_model=ctx.embedding_model,
        filters=SearchFilters(kinds=("transcript",), allowed_sources=(rec.source_item_id,)),
    )
    for h in hits:
        ctx.note_candidate(f"chunk:{h.id}", "hybrid" if h.in_fts and h.cosine is not None else "fts", h.rrf)
    return [cards.chunk_card(h, ctx.tz, score=h.rrf, priority="evidence") for h in hits[:TRANSCRIPT_CHUNKS]]


async def meeting_transcript(ctx: Ctx) -> Retrieved:
    """T1 (AI-06): "what did X say about Y" over the meeting's transcript windows and items."""
    m = await _session_meeting(ctx)
    if m is None:
        return Retrieved([], False)
    chunks = await _transcript_hits(ctx, m, ctx.plan.topic or m.title or "")
    rec = await meetings.recording_for_meeting(ctx.uow, m.id)
    mw = await work.meeting_work(ctx.uow, meeting_id=m.id, source_item_id=rec.source_item_id if rec else None)
    items = chunks + await _item_cards(ctx, mw.items[:10])
    return Retrieved(items, bool(chunks))


def summary_card(m: meetings.MeetingRecord) -> PacketItem | None:
    """The AI-10 summary (an inference) with topics and concerns."""
    if not m.summary:
        return None
    topics = ", ".join(m.summary.get("topics") or [])
    concerns = "; ".join(c.get("text", "") for c in m.summary.get("concerns") or [])
    body = delimit(m.summary.get("text") or "")
    text = f"[meeting summary · AI-derived · {m.summary_model or 'model'}] {body}"
    if topics:
        text += f" · topics: {delimit(topics)}"
    if concerns:
        text += f" · concerns raised: {delimit(concerns)}"
    return PacketItem(
        key=f"summary:{m.id}",
        kind="summary",
        section="anchors",
        priority="anchor",
        text=text,
        line=f"AI summary of {m.title or 'the meeting'}",
        data_class="ai_derived",
        claim_kind="inference",
        authority=2,
        entity_id=m.id,
        source_item_ids=(m.source_item_id,),
        as_of=m.summary_derived_at,
        score=10.0,
    )


async def meeting_synthesis(ctx: Ctx) -> Retrieved:
    """T2 (AI-07): "what concerns were raised?" over summary, concerns, decisions, items, chunks."""
    m = await _session_meeting(ctx)
    if m is None:
        return Retrieved([], False)
    items: list[PacketItem] = []
    summary = summary_card(m)
    if summary is not None:
        items.append(summary)
    rec = await meetings.recording_for_meeting(ctx.uow, m.id)
    mw = await work.meeting_work(ctx.uow, meeting_id=m.id, source_item_id=rec.source_item_id if rec else None)
    items += await _decision_cards(ctx, mw.decisions + mw.open_questions, anchor=False)
    items += await _item_cards(ctx, mw.items[:15])
    items += await _transcript_hits(ctx, m, ctx.plan.topic or m.title or "")
    ctx.focus.append(FocusEntry("meeting", m.id, m.title or "meeting", 0))
    return Retrieved(items, bool(items))


MEETING_RETRIEVERS = {
    "meeting_prep": meeting_prep,
    "cross_meeting": cross_meeting,
    "meeting_lookup": meeting_lookup,
    "meeting_transcript": meeting_transcript,
    "meeting_synthesis": meeting_synthesis,
}
