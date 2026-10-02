"""Typed retrievers per scenario (CONTEXT_ARCHITECTURE.md §10): relational recipes of at most two
hops, temporal filters, and hybrid discovery only where the intent is topical (§6.1 rule).

Each retriever returns packet candidates (cards with section and packing priority), whether
anything matched (the abstention pre-check, AI_PIPELINE.md §5.7) and content-free trace entries.
The permission scope (§9.7) is applied in every statement: explicit ``user_id`` predicates in the
module read functions, the visible-source subquery in chunk search and quote selection, the
session's allowed sources, and verification rules (rejected excluded, suggested labelled,
archived only on request).
"""

from __future__ import annotations

import datetime
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from eca import communication, ingestion, meetings, people, work
from eca.communication import ConversationSummary
from eca.people import PersonRef
from eca.platform.uow import UnitOfWork
from eca.retrieval import cards, ranking
from eca.retrieval.packet import LIST_LIMIT, PacketItem, Priority, Section
from eca.retrieval.plan import FocusEntry, Plan, SessionState
from eca.retrieval.search import ChunkHit, SearchFilters, hybrid_search, ts_terms
from eca.retrieval.temporal import TimeWindow
from eca.work import TimelineEvent, WorkItemView

AMBIGUITY_MARGIN = 0.10  # two candidates within 10% → ask or choose by focus (§10.2)
PERSON_TIMELINE_DAYS = 30
TOPIC_WINDOW_DAYS = 60
EMAIL_CONTEXT_WINDOW_DAYS = 30
NEEDS_RESPONSE_DAYS = 14
QUOTES_PER_ITEM = 2
CHAIN_QUOTES_PER_ITEM = 8
MAX_ANCHOR_ITEMS = 6
PROMISE_MIN_CONFIDENCE = 0.7  # suggestion/inferred commitments listed only above this (§10.8)
NEXT_ACTION_PRIORITY = 60.0
OPEN = ("open", "in_progress")


@dataclass
class Ctx:
    """What every retriever needs; built once per assembly."""

    uow: UnitOfWork
    plan: Plan
    session: SessionState
    now: datetime.datetime
    tz: ZoneInfo
    self_id: UUID
    filters: SearchFilters
    window: TimeWindow | None
    query_vector: str | None
    embedding_model: str | None
    persons: dict[UUID, PersonRef] = field(default_factory=dict)
    trace: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    focus: list[FocusEntry] = field(default_factory=list)

    async def load_persons(self, ids: Sequence[UUID | None]) -> dict[UUID, PersonRef]:
        wanted = sorted({i for i in ids if i is not None and i not in self.persons})
        if wanted:
            self.persons.update(await people.get_persons(self.uow, wanted))
        return self.persons

    def in_scope(self, sources: Sequence[UUID]) -> bool:
        allowed = self.filters.allowed_sources
        return allowed is None or any(s in allowed for s in sources)

    def note_candidate(self, key: str, method: str, score: float) -> None:
        self.trace.append({"key": key, "method": method, "score": round(score, 6)})


@dataclass
class Retrieved:
    items: list[PacketItem]
    matched: bool
    window: TimeWindow | None = None
    clarification: str | None = None
    unresolved: list[str] = field(default_factory=list)


Retriever = Callable[[Ctx], Awaitable[Retrieved]]


# ---------------------------------------------------------------- helpers


def working_days_before(now: datetime.datetime, days: int, tz: ZoneInfo) -> datetime.datetime:
    """``days`` working days (Monday-Friday) before ``now`` (no holiday calendar, §7.1)."""
    moment = now.astimezone(tz)
    left = days
    while left > 0:
        moment -= datetime.timedelta(days=1)
        if moment.weekday() < 5:
            left -= 1
    return moment.astimezone(datetime.UTC)


@dataclass(frozen=True)
class PersonResolution:
    ids: list[UUID]  # resolved persons and everyone merged into them (CC-41)
    anchors: list[PersonRef]
    clarification: str | None
    unresolved: list[str]


async def resolve_people(ctx: Ctx) -> PersonResolution:
    """Names (or a pronoun) → persons; ambiguity → focus choice or a clarification question."""
    anchors: list[PersonRef] = []
    unresolved: list[str] = []
    names = list(ctx.plan.person_names)
    if not names and ctx.plan.pronoun == "person":
        focused = next((f for f in ctx.session.focus if f.type == "person"), None)
        if focused is not None:
            got = await people.get_persons(ctx.uow, [focused.id])
            if focused.id in got:
                anchors.append(got[focused.id])
                ctx.notes.append(
                    f"'{ctx.plan.pronoun}' was read as {focused.label} (from this conversation)."
                )
    for name in names:
        matches = await people.match_names(ctx.uow, name)
        if not matches:
            unresolved.append(name)
            continue
        top = matches[0]
        close = [m for m in matches[1:] if m.score >= top.score * (1 - AMBIGUITY_MARGIN)]
        if close:
            focus_ids = {f.id for f in ctx.session.focus if f.type == "person"}
            chosen = next((m for m in [top, *close] if m.person.id in focus_ids), None)
            if chosen is None:
                options = "; ".join(_describe(m.person) for m in [top, *close])
                return PersonResolution([], [], f'Which "{name}" do you mean: {options}?', unresolved)
            ctx.notes.append(f'"{name}" was read as {_describe(chosen.person)} (from this conversation).')
            top = chosen
        anchors.append(top.person)
    ids: list[UUID] = []
    for p in anchors:
        ids.extend(await people.merged_ids(ctx.uow, p.id))
        ctx.focus.append(FocusEntry("person", p.id, p.display_name or p.primary_email or "person", 0))
        ctx.persons[p.id] = p
    return PersonResolution(sorted(set(ids)), anchors, None, unresolved)


def _describe(p: PersonRef) -> str:
    name = p.display_name or p.primary_email or "unknown"
    return f"{name} <{p.primary_email}>" if p.primary_email and p.display_name else name


async def _item_cards(
    ctx: Ctx,
    items: Sequence[WorkItemView],
    *,
    section: Section = "state",
    priority: Priority = "other_state",
    ordered: bool = True,
    scores: dict[UUID, float] | None = None,
) -> list[PacketItem]:
    """Cards in the given order (``ordered``: descending scores keep list order in the packet)."""
    await ctx.load_persons(
        [p for i in items for p in (i.owner_person_id, i.counterparty_person_id, i.requester_person_id)]
    )
    out = []
    for n, item in enumerate(items):
        score = (scores or {}).get(item.id, float(len(items) - n) if ordered else 1.0)
        out.append(
            cards.item_card(
                item,
                ctx.persons,
                ctx.self_id,
                ctx.tz,
                ctx.now,
                section=section,
                priority=priority,
                score=score,
            )
        )
        ctx.note_candidate(f"work_item:{item.id}", "sql", score)
    return out


async def _quote_cards(
    ctx: Ctx, item_type: str, item_ids: Sequence[UUID], *, per_item: int, priority: Priority = "evidence"
) -> list[PacketItem]:
    """Live, visible evidence quotes for items (trashed or out-of-scope sources left out)."""
    by_item = await work.evidence_for(ctx.uow, item_type, list(item_ids), per_item=per_item)
    sources = sorted({e.source_item_id for quotes in by_item.values() for e in quotes})
    visible = await ingestion.visible_among(
        ctx.uow, sources, exclude_calendar_connections=ctx.filters.exclude_calendar_connections
    )
    senders = await communication.sender_of_sources(ctx.uow, [s for s in sources if s in visible])
    await ctx.load_persons(list(senders.values()))
    out = []
    for item_id, quotes in by_item.items():
        for e in quotes:
            if e.source_item_id not in visible or not ctx.in_scope([e.source_item_id]):
                continue
            sender = senders.get(e.source_item_id)
            origin = (
                f"email from {cards.person_label(sender, ctx.persons, ctx.self_id)}" if sender else "source"
            )
            out.append(
                cards.quote_card(
                    e, ctx.tz, origin=origin, item_key=f"{item_type}:{item_id}", priority=priority
                )
            )
    return out


async def _timeline_cards(ctx: Ctx, item_ids: Sequence[UUID]) -> list[PacketItem]:
    """Full event chains of anchor items (chain completeness beats diversity, §9.2 A14)."""
    events = await work.events_for(ctx.uow, "work_item", list(item_ids))
    await ctx.load_persons(
        [UUID(str(e.payload["by_person"])) for evs in events.values() for e in evs if _person_ref(e)]
    )
    out = []
    for item_id, evs in events.items():
        for ev in evs:
            out.append(
                cards.event_card(ev, ctx.persons, ctx.self_id, ctx.tz, item_key=f"work_item:{item_id}")
            )
    return out


def _person_ref(ev: TimelineEvent) -> bool:
    by = ev.payload.get("by_person")
    if not by:
        return False
    try:
        UUID(str(by))
    except ValueError:
        return False
    return True


def _due_order(now: datetime.datetime) -> Callable[[WorkItemView], tuple[int, datetime.datetime, float, str]]:
    far = datetime.datetime.max.replace(tzinfo=datetime.UTC)

    def key(i: WorkItemView) -> tuple[int, datetime.datetime, float, str]:
        overdue = 0 if (i.due_at is not None and i.due_at < now) else 1
        return (overdue, i.due_at or far, -(i.priority_score or 0.0), str(i.id))

    return key


async def _open_items(
    ctx: Ctx, directions: Sequence[str], *, person_id: UUID | None = None
) -> list[WorkItemView]:
    out: list[WorkItemView] = []
    for direction in directions:
        page = await work.list_items_page(
            ctx.uow, direction=direction, status="open", person_id=person_id, sort="due", limit=LIST_LIMIT
        )
        out.extend(page.items)
    return [i for i in out if ctx.in_scope(i.evidence_source_ids) or i.origin == "user"]


async def _conversation_cards(
    ctx: Ctx,
    convs: Sequence[ConversationSummary],
    *,
    priority: Priority = "other_state",
    ordered: bool = True,
) -> list[PacketItem]:
    latest = await communication.latest_messages(ctx.uow, [c.id for c in convs])
    await ctx.load_persons([m.sender_person_id for m in latest.values()])
    out = []
    for n, c in enumerate(convs):
        msg = latest.get(c.id)
        sender = cards.person_label(msg.sender_person_id, ctx.persons, ctx.self_id) if msg else None
        score = float(len(convs) - n) if ordered else 1.0
        out.append(
            cards.conversation_card(
                c,
                ctx.tz,
                sender=sender,
                sources=(msg.source_item_id,) if msg else (),
                priority=priority,
                score=score,
            )
        )
        ctx.note_candidate(f"conversation:{c.id}", "sql", score)
    return out


def _unresolved_note(ctx: Ctx, names: list[str]) -> None:
    for name in names:
        ctx.notes.append(f'No person named "{name}" was found in your contacts.')


# ---------------------------------------------------------------- S7, S8, S9 and list intents


async def waiting_for(ctx: Ctx) -> Retrieved:
    """S7 (§10.7): items others owe the user, plus implicit waiting-for threads."""
    who = await resolve_people(ctx)
    if who.clarification:
        return Retrieved([], False, clarification=who.clarification)
    items = await _open_items(ctx, ("waiting_for", "delegated"))
    if who.ids:
        items = [i for i in items if i.owner_person_id in who.ids]
    items.sort(key=_due_order(ctx.now))
    found = await _item_cards(ctx, items[:LIST_LIMIT])
    if not who.ids and not ctx.plan.person_names:
        quiet = working_days_before(ctx.now, 1, ctx.tz)
        implicit = await communication.waiting_on_others(ctx.uow, quiet_since=quiet, limit=20)
        convs = await _conversation_cards(ctx, implicit, ordered=False)
        found += [_as_waiting(c) for c in convs]
    _unresolved_note(ctx, who.unresolved)
    return Retrieved(found, bool(found), unresolved=who.unresolved)


def _as_waiting(card: PacketItem) -> PacketItem:
    """An implicit waiting-for thread: the user asked and nobody answered (§10.7 step 2)."""
    return replace(
        card, score=0.0, line=card.line.replace("awaiting the other side", "no answer to your request")
    )


def _strength_ok(i: WorkItemView) -> bool:
    if i.origin == "user" or i.verification_status in ("confirmed", "user_created"):
        return True
    if i.commitment_strength in ("explicit", "probable"):
        return True
    return (i.confidence or 0.0) >= PROMISE_MIN_CONFIDENCE


async def promised(ctx: Ctx) -> Retrieved:
    """S8 (§10.8): the user's commitments; unmapped-speaker items listed apart for confirmation."""
    who = await resolve_people(ctx)
    if who.clarification:
        return Retrieved([], False, clarification=who.clarification)
    items = [i for i in await _open_items(ctx, ("my_commitment",)) if _strength_ok(i)]
    if who.ids:
        items = [i for i in items if i.counterparty_person_id in who.ids or i.requester_person_id in who.ids]
    items.sort(key=_due_order(ctx.now))
    found = await _item_cards(ctx, items[:LIST_LIMIT])
    unresolved_items = await _open_items(ctx, ("unresolved",))
    if unresolved_items and not who.ids:
        extra = await _item_cards(ctx, unresolved_items[:10], ordered=False)
        found += [replace(c, score=0.0, group="possible — confirm the speaker") for c in extra]
    _unresolved_note(ctx, who.unresolved)
    return Retrieved(found, bool(found), unresolved=who.unresolved)


async def needs_response(ctx: Ctx) -> Retrieved:
    """S9 (§10.9): threads awaiting the user plus open requests to the user, by priority."""
    page = await communication.needs_response_page(ctx.uow, after=None, limit=LIST_LIMIT)
    cutoff = ctx.now - datetime.timedelta(days=NEEDS_RESPONSE_DAYS)
    convs = [c for c in page.items if c.last_inbound_at is None or c.last_inbound_at >= cutoff]
    who = await resolve_people(ctx)
    if who.clarification:
        return Retrieved([], False, clarification=who.clarification)
    requests = [i for i in await _open_items(ctx, ("my_task",)) if i.type == "request"]
    if who.ids:
        latest = await communication.latest_messages(ctx.uow, [c.id for c in convs])
        convs = [c for c in convs if (m := latest.get(c.id)) is not None and m.sender_person_id in who.ids]
        requests = [i for i in requests if i.requester_person_id in who.ids]
    conv_cards = await _conversation_cards(ctx, convs, ordered=False)
    item_cards = await _item_cards(ctx, requests, ordered=False)
    prio = {c.id: c.priority_score or 0.0 for c in convs} | {i.id: i.priority_score or 0.0 for i in requests}
    merged = sorted(
        conv_cards + item_cards, key=lambda p: (-(prio.get(p.entity_id, 0.0) if p.entity_id else 0.0), p.key)
    )
    found = [replace(p, score=float(len(merged) - n)) for n, p in enumerate(merged)]
    _unresolved_note(ctx, who.unresolved)
    return Retrieved(found, bool(found), unresolved=who.unresolved)


async def who_waiting_on_me(ctx: Ctx) -> Retrieved:
    """§10.12a: commitments, requests and threads the user owes, grouped by person."""
    items = await _open_items(ctx, ("my_commitment", "my_task"))
    items = [i for i in items if _strength_ok(i)]
    items.sort(key=lambda i: (-(i.priority_score or 0.0), str(i.id)))
    item_cards = await _item_cards(ctx, items[:LIST_LIMIT])
    page = await communication.needs_response_page(ctx.uow, after=None, limit=20)
    conv_cards = await _conversation_cards(ctx, page.items, ordered=False)
    found = item_cards + conv_cards
    return Retrieved(found, bool(found))


async def overdue(ctx: Ctx) -> Retrieved:
    """§10.12a: open items past due, both directions (observed and unresolved left out)."""
    items = [
        i
        for i in await _open_items(ctx, ("my_commitment", "my_task", "waiting_for", "delegated"))
        if i.due_at is not None and i.due_at < ctx.now and i.due_precision != "fuzzy"
    ]
    items.sort(key=_due_order(ctx.now))
    found = await _item_cards(ctx, items[:LIST_LIMIT])
    return Retrieved(found, bool(found))


async def deadlines(ctx: Ctx) -> Retrieved:
    """Deadlines in the window (default: the next 7 days), overdue ones flagged first."""
    window = ctx.window or TimeWindow(
        ctx.now, ctx.now + datetime.timedelta(days=7), "the next 7 days", "default"
    )
    items = [
        i
        for i in await _open_items(ctx, ("my_commitment", "my_task", "waiting_for", "delegated"))
        if i.due_at is not None and i.due_at < window.end and (i.due_at >= window.start or i.due_at < ctx.now)
    ]
    items.sort(key=_due_order(ctx.now))
    found = await _item_cards(ctx, items[:LIST_LIMIT])
    return Retrieved(found, bool(found), window=window)


# ---------------------------------------------------------------- S2 person context


async def person_context(ctx: Ctx) -> Retrieved:
    """S2 (§10.2): card, open items both directions, awaiting threads, 30-day interactions,
    next and last meeting. Answered by AI-06 over this packet."""
    who = await resolve_people(ctx)
    if who.clarification:
        return Retrieved([], False, clarification=who.clarification)
    if not who.anchors:
        _unresolved_note(ctx, who.unresolved)
        return Retrieved([], False, unresolved=who.unresolved)
    anchor = who.anchors[0]
    involved: list[WorkItemView] = []
    for pid in who.ids:
        page = await work.list_items_page(ctx.uow, person_id=pid, status="open", sort="due", limit=LIST_LIMIT)
        involved.extend(page.items)
    involved = list(
        {i.id: i for i in involved if ctx.in_scope(i.evidence_source_ids) or i.origin == "user"}.values()
    )
    involved.sort(key=_due_order(ctx.now))
    you_owe = sum(1 for i in involved if i.direction in ("my_commitment", "my_task"))
    they_owe = sum(1 for i in involved if i.direction in ("waiting_for", "delegated"))
    since = ctx.now - datetime.timedelta(days=PERSON_TIMELINE_DAYS)
    upcoming = await meetings.meeting_details_between(
        ctx.uow, ctx.now, ctx.now + datetime.timedelta(days=14), person_ids=who.ids, limit=3
    )
    past = await meetings.meeting_details_between(ctx.uow, since, ctx.now, person_ids=who.ids, limit=10)
    detail = await people.person_detail(ctx.uow, anchor.id)
    org = (detail.organization or {}).get("name") if detail.organization else None
    next_meeting = upcoming[0].title if upcoming else None
    items: list[PacketItem] = [
        cards.person_card(
            anchor,
            ctx.tz,
            role=detail.person.role_title,
            organization=org,
            last_interaction_at=detail.person.last_interaction_at,
            you_owe=you_owe,
            they_owe=they_owe,
            next_meeting=next_meeting,
            score=10.0,
        )
    ]
    items += await _item_cards(ctx, involved[:20], priority="anchor")
    items += await _quote_cards(ctx, "work_item", [i.id for i in involved[:10]], per_item=1)
    threads = await communication.conversations_with_people(ctx.uow, who.ids, since=since, limit=10)
    items += await _conversation_cards(ctx, threads)
    mentioned = await people.mentions_of(ctx.uow, "person", who.ids, since=since)
    if mentioned:
        ctx.notes.append(
            f"Mentioned by name in {len(mentioned)} source(s) in the last {PERSON_TIMELINE_DAYS} days."
        )
    await ctx.load_persons([p for m in [*upcoming, *past] for p in m.attendee_ids])
    for m in [*upcoming[:1], *past[-2:]]:
        items.append(
            cards.meeting_card(
                m, ctx.persons, ctx.self_id, ctx.tz, ctx.now, section="state", priority="other_state"
            )
        )
    _unresolved_note(ctx, who.unresolved)
    return Retrieved(items, True, unresolved=who.unresolved)


# ---------------------------------------------------------------- S6 cross-email status


async def _discover(
    ctx: Ctx, topic: str, *, since: datetime.datetime, person_ids: Sequence[UUID] = ()
) -> list[ChunkHit]:
    filters = SearchFilters(
        since=since,
        person_ids=tuple(person_ids),
        allowed_sources=ctx.filters.allowed_sources,
        exclude_calendar_connections=ctx.filters.exclude_calendar_connections,
    )
    hits = await hybrid_search(
        ctx.uow,
        terms=ts_terms(topic),
        query_vector=ctx.query_vector,
        embedding_model=ctx.embedding_model,
        filters=filters,
    )
    for h in hits:
        ctx.note_candidate(
            f"chunk:{h.id}",
            "hybrid" if h.in_fts and h.cosine is not None else ("fts" if h.in_fts else "vector"),
            h.rrf,
        )
    return hits


async def topic_status(ctx: Ctx) -> Retrieved:
    """S6 (§10.6): discover anchor items (item FTS, then chunks pivoted through evidence), expand
    each anchor's full timeline, add continuation threads and decisions; status first."""
    topic = ctx.plan.topic or ctx.plan.qualifier or ""
    who = await resolve_people(ctx)
    if who.clarification:
        return Retrieved([], False, clarification=who.clarification)
    since = ctx.now - datetime.timedelta(days=TOPIC_WINDOW_DAYS)
    window = ctx.window or TimeWindow(since, ctx.now, f"the last {TOPIC_WINDOW_DAYS} days", "default")
    terms = ts_terms(topic)
    found_items = await work.search_items(ctx.uow, terms, limit=10, include_closed=True)
    found_decisions = await work.search_decisions(ctx.uow, terms, limit=6)
    hits = await _discover(ctx, topic, since=window.start, person_ids=who.ids)
    pivot_items, pivot_decisions = await work.item_ids_for_sources(ctx.uow, [h.source_item_id for h in hits])
    scores: dict[UUID, float] = {}
    for item, rank in found_items:
        scores[item.id] = max(scores.get(item.id, 0.0), rank)
        ctx.note_candidate(f"work_item:{item.id}", "fts", rank)
    hit_rrf = {h.source_item_id: h.rrf for h in hits}
    for item_id, sources in pivot_items.items():
        best = max(hit_rrf.get(s, 0.0) for s in sources)
        scores[item_id] = max(scores.get(item_id, 0.0), best * 10)
        ctx.note_candidate(f"work_item:{item_id}", "pivot", best)
    anchors_all = await work.items_by_ids(ctx.uow, list(scores))
    anchors_all = [i for i in anchors_all if ctx.in_scope(i.evidence_source_ids) or i.origin == "user"]
    if who.ids:
        anchors_all = [
            i
            for i in anchors_all
            if {i.owner_person_id, i.counterparty_person_id, i.requester_person_id} & set(who.ids)
        ] or anchors_all
    ranked = sorted(
        anchors_all,
        key=lambda i: (
            -ranking.score(
                base=scores.get(i.id, 0.0) or 0.01,
                kind="open_item" if i.lifecycle_status in OPEN else "decision",
                age=ctx.now - (i.last_activity_at or i.derived_at),
                authority=ranking.authority_class(
                    origin=i.origin,
                    verification_status=i.verification_status,
                    user_fields=bool(i.user_fields),
                    strength=i.commitment_strength,
                ),
            ),
            str(i.id),
        ),
    )
    anchors = ranked[:MAX_ANCHOR_ITEMS]
    anchor_scores = {i.id: float(len(anchors) - n) * 10 for n, i in enumerate(anchors)}
    items: list[PacketItem] = await _item_cards(ctx, anchors, priority="anchor", scores=anchor_scores)
    items += await _timeline_cards(ctx, [i.id for i in anchors])
    items += await _quote_cards(
        ctx, "work_item", [i.id for i in anchors], per_item=CHAIN_QUOTES_PER_ITEM, priority="anchor_timeline"
    )
    for i in anchors:
        ctx.focus.append(FocusEntry("work_item", i.id, i.title, 0))
    decision_ids = {d.id for d, _ in found_decisions} | set(pivot_decisions)
    decisions_found = await work.decisions_by_ids(ctx.uow, sorted(decision_ids))
    for d in decisions_found:
        items.append(cards.decision_card(d, ctx.tz, sources=tuple(sorted(pivot_decisions.get(d.id, set())))))
        ctx.note_candidate(f"decision:{d.id}", "fts", 1.0)
    chain_sources = sorted({s for i in anchors for s in i.evidence_source_ids})
    conv_of = await communication.conversation_ids_of_sources(ctx.uow, chain_sources)
    links = await work.links_of(ctx.uow, "conversation", sorted(set(conv_of.values())), relation="continues")
    linked = sorted(({lk.to_id for lk in links} | {lk.from_id for lk in links}) - set(conv_of.values()))
    if linked:
        convs = await communication.conversations_by_ids(ctx.uow, linked)
        items += await _conversation_cards(ctx, convs, ordered=False)
    anchored_sources = set(chain_sources)
    for h in hits:
        if h.source_item_id in anchored_sources:
            continue
        age = ctx.now - h.occurred_at
        s = ranking.score(
            base=h.rrf,
            kind="message" if h.kind == "email" else h.kind,
            age=age,
            anchor_match=bool(set(h.person_ids) & set(who.ids)),
        )
        items.append(cards.chunk_card(h, ctx.tz, score=s))
    if not anchors and any(h.is_match for h in hits):
        ctx.notes.append("Found only in discussion: no tracked item matches this topic.")
    matched = bool(anchors) or bool(decisions_found) or any(h.is_match for h in hits)
    _unresolved_note(ctx, who.unresolved)
    return Retrieved(items, matched, window=window, unresolved=who.unresolved)


# ---------------------------------------------------------------- S12 next action


async def next_action(ctx: Ctx) -> Retrieved:
    """S12 (§10.12): the active working set (overdue and due-today items of the user, high-priority
    replies, a meeting within 2 h) and the free time until the next meeting."""
    local_now = ctx.now.astimezone(ctx.tz)
    end_of_day = (
        local_now.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)
    ).astimezone(datetime.UTC)
    mine = [
        i
        for i in await _open_items(ctx, ("my_commitment", "my_task"))
        if (i.due_at is not None and i.due_at < end_of_day)
        or (i.priority_score or 0.0) >= NEXT_ACTION_PRIORITY
    ]
    mine.sort(key=lambda i: (-(i.priority_score or 0.0), i.due_at or end_of_day, str(i.id)))
    items = await _item_cards(ctx, mine[:10], priority="anchor")
    items += await _quote_cards(ctx, "work_item", [i.id for i in mine[:5]], per_item=1)
    page = await communication.needs_response_page(ctx.uow, after=None, limit=10)
    urgent = [c for c in page.items if (c.priority_score or 0.0) >= NEXT_ACTION_PRIORITY]
    items += await _conversation_cards(ctx, urgent, priority="anchor")
    soon = await meetings.meeting_details_between(
        ctx.uow, ctx.now, ctx.now + datetime.timedelta(hours=8), limit=5
    )
    await ctx.load_persons([p for m in soon for p in m.attendee_ids])
    for m in soon[:2]:
        items.append(
            cards.meeting_card(
                m, ctx.persons, ctx.self_id, ctx.tz, ctx.now, section="state", priority="anchor"
            )
        )
    if soon:
        free = soon[0].starts_at - ctx.now
        minutes = max(int(free.total_seconds() // 60), 0)
        ctx.notes.append(
            f"Free time until the next meeting: about {minutes} minutes "
            f"({cards.fmt_date(soon[0].starts_at, ctx.tz, with_time=True)})."
        )
    else:
        ctx.notes.append("No meeting in the next 8 hours.")
    return Retrieved(items, bool(mine or urgent))


# ---------------------------------------------------------------- S1 current email


async def email_context(ctx: Ctx) -> Retrieved:
    """S1 (§10.1): thread state, its items, open items with the sender, meetings with the sender,
    continuation threads and decisions; FTS fallback when fewer than 3 items are found."""
    conversation_id = ctx.plan.conversation_id
    if conversation_id is None:
        return Retrieved([], False)
    convs = await communication.conversations_by_ids(ctx.uow, [conversation_id])
    if not convs:
        return Retrieved([], False)
    conv = convs[0]
    sources = (await communication.conversation_sources(ctx.uow, [conversation_id])).get(conversation_id, [])
    items: list[PacketItem] = await _conversation_cards(ctx, [conv], priority="anchor")
    ctx.focus.append(FocusEntry("conversation", conv.id, conv.subject or "thread", 0))
    from_thread, decisions_of = await work.item_ids_for_sources(ctx.uow, sources)
    thread_items = await work.items_by_ids(ctx.uow, list(from_thread))
    items += await _item_cards(ctx, thread_items, priority="anchor")
    latest = await communication.latest_messages(ctx.uow, [conversation_id])
    sender = latest[conversation_id].sender_person_id if conversation_id in latest else None
    shared: list[WorkItemView] = []
    if sender is not None:
        page = await work.list_items_page(ctx.uow, person_id=sender, status="open", sort="priority", limit=5)
        shared = [i for i in page.items if i.id not in from_thread]
        items += await _item_cards(ctx, shared)
        meet = await meetings.meeting_details_between(
            ctx.uow, ctx.now, ctx.now + datetime.timedelta(days=14), person_ids=[sender], limit=1
        )
        meet += await meetings.meeting_details_between(
            ctx.uow, ctx.now - datetime.timedelta(days=60), ctx.now, person_ids=[sender], limit=1
        )
        await ctx.load_persons([p for m in meet for p in m.attendee_ids])
        items += [
            cards.meeting_card(m, ctx.persons, ctx.self_id, ctx.tz, ctx.now, section="state") for m in meet
        ]
    links = await work.links_of(ctx.uow, "conversation", [conversation_id], relation="continues")
    linked = sorted(({lk.to_id for lk in links} | {lk.from_id for lk in links}) - {conversation_id})
    if linked:
        items += await _conversation_cards(
            ctx, await communication.conversations_by_ids(ctx.uow, linked), ordered=False
        )
    thread_decisions = {d.id: d for d in await work.decisions_for_conversations(ctx.uow, [conversation_id])}
    for d in await work.decisions_by_ids(ctx.uow, sorted(decisions_of)):
        thread_decisions.setdefault(d.id, d)
    for d in thread_decisions.values():
        items.append(cards.decision_card(d, ctx.tz, sources=tuple(sorted(decisions_of.get(d.id, set())))))
    if len(thread_items) + len(shared) < 3 and conv.subject:
        hits = await _discover(
            ctx,
            conv.subject,
            since=ctx.now - datetime.timedelta(days=EMAIL_CONTEXT_WINDOW_DAYS),
            person_ids=[sender] if sender else (),
        )
        others = [h for h in hits if h.conversation_id != conversation_id][:3]
        items += [cards.chunk_card(h, ctx.tz, score=h.rrf) for h in others]
    return Retrieved(items, True)


async def unsupported(ctx: Ctx) -> Retrieved:
    return Retrieved([], False)


RETRIEVERS: dict[str, Retriever] = {
    "waiting_for": waiting_for,
    "promised": promised,
    "needs_response": needs_response,
    "who_waiting_on_me": who_waiting_on_me,
    "overdue": overdue,
    "deadlines": deadlines,
    "person": person_context,
    "topic_status": topic_status,
    "next_action": next_action,
    "email_context": email_context,
    "unsupported": unsupported,
}
