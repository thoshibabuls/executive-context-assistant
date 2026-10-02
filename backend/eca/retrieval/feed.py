"""S10 "What happened yesterday?" and S11 "What changed?" (CONTEXT_ARCHITECTURE.md §10.10-§10.11):
the chat retrievers and the services behind ``GET /api/v1/changes`` and ``GET /api/v1/days/{date}``.

Deterministic. The change anchor is an explicit time, else the checkpoint of the surface (in
chat: ``chat``, else ``today``; §13), else the last 7 days (§7.1). Reading never moves a
checkpoint; the API client and the chat answer do (``identity.mark_seen``).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, replace
from uuid import UUID
from zoneinfo import ZoneInfo

from eca import connections, identity, people, work
from eca.platform.uow import UnitOfWork
from eca.retrieval import cards, temporal
from eca.retrieval.changes import ChangeSet, DayView, NetChange, changes_since, day_view
from eca.retrieval.coverage import Coverage, build_coverage
from eca.retrieval.packet import PacketItem
from eca.retrieval.retrievers import Ctx, Retrieved, resolve_people
from eca.retrieval.temporal import TimeWindow

MAX_CHANGES_IN_PACKET = 40
GROUP_LABEL = {
    "new_mine": "new: you owe",
    "new_theirs": "new: owed to you",
    "new_other": "new: other",
    "status_changes": "status changed",
    "deadline_changes": "deadline changed",
    "conflicts": "conflicting information",
    "decisions": "decisions",
    "awaiting_replies": "now awaiting your reply",
    "overdue_or_stale": "overdue or stale",
    "meetings": "meetings held",
}


def _describe_values(values: dict[str, object], tz: ZoneInfo) -> str:
    parts = []
    for name, value in values.items():
        if name == "due_at" and isinstance(value, str):
            value = cards.fmt_date(datetime.datetime.fromisoformat(value), tz)
        parts.append(f"{name.replace('_', ' ')} {value if value is not None else 'none'}")
    return ", ".join(parts)


def change_cards(
    cs: ChangeSet,
    ctx_tz: ZoneInfo,
    now: datetime.datetime,
    persons: dict[UUID, people.PersonRef],
    self_id: UUID,
    *,
    limit: int | None = MAX_CHANGES_IN_PACKET,
) -> list[PacketItem]:
    """One card per net change, grouped and ranked (§10.11 steps 4-5)."""
    out: list[PacketItem] = []
    chosen = cs.changes if limit is None else cs.changes[:limit]
    for n, ch in enumerate(chosen):
        score = float(len(chosen) - n)
        label = GROUP_LABEL.get(ch.group, ch.group)
        if ch.entity_type == "work_item" and ch.entity_id in cs.items:
            card = cards.item_card(
                cs.items[ch.entity_id], persons, self_id, ctx_tz, now, priority="anchor", score=score
            )
            delta = ""
            if ch.before or ch.after:
                delta = (
                    f" · change: {_describe_values(ch.before, ctx_tz)} → {_describe_values(ch.after, ctx_tz)}"
                )
            extra = f" · {'; '.join(ch.detail)}" if ch.detail else ""
            out.append(replace(card, text=f"[{label}] {card.text}{delta}{extra}", group=label))
        elif ch.entity_type == "decision" and ch.entity_id in cs.decisions:
            card = cards.decision_card(cs.decisions[ch.entity_id], ctx_tz, priority="anchor", score=score)
            out.append(replace(card, text=f"[{label}] {card.text}", group=label))
        elif ch.entity_type == "conversation" and ch.entity_id in cs.conversations:
            card = cards.conversation_card(
                cs.conversations[ch.entity_id], ctx_tz, priority="anchor", score=score
            )
            out.append(replace(card, text=f"[{label}] {card.text}", group=label))
        elif ch.entity_type == "meeting" and ch.entity_id in cs.meetings:
            card = cards.meeting_card(
                cs.meetings[ch.entity_id],
                persons,
                self_id,
                ctx_tz,
                now,
                section="state",
                priority="anchor",
                score=score,
            )
            out.append(replace(card, text=f"[{label}] {card.text}", group=label))
    return out


async def change_anchor(
    uow: UnitOfWork, *, now: datetime.datetime, surfaces: tuple[str, ...], explicit: TimeWindow | None
) -> TimeWindow:
    if explicit is not None:
        return explicit
    seen = await identity.get_checkpoints(uow, list(surfaces))
    last = next((seen[s] for s in surfaces if s in seen), None)
    return temporal.from_checkpoint(last, now)


async def _persons_for(uow: UnitOfWork, cs: ChangeSet) -> dict[UUID, people.PersonRef]:
    ids = {
        p
        for v in cs.items.values()
        for p in (v.owner_person_id, v.counterparty_person_id, v.requester_person_id)
        if p
    }
    ids |= {p for m in cs.meetings.values() for p in m.attendee_ids}
    return await people.get_persons(uow, sorted(ids))


async def what_changed(ctx: Ctx) -> Retrieved:
    """S11 chat retriever: net changes since the anchor, as ranked cards for AI-07."""
    who = await resolve_people(ctx)
    if who.clarification:
        return Retrieved([], False, clarification=who.clarification)
    explicit = ctx.window if ctx.plan.since or ctx.plan.time_expression else None
    if explicit is not None and not ctx.plan.since:
        explicit = TimeWindow(explicit.start, ctx.now, f"since {explicit.label}", "explicit")
    window = await change_anchor(ctx.uow, now=ctx.now, surfaces=("chat", "today"), explicit=explicit)
    if window.note:
        ctx.notes.append(window.note)
    cs = await changes_since(ctx.uow, anchor=window.start, now=ctx.now, person_ids=who.ids)
    ctx.persons.update(await _persons_for(ctx.uow, cs))
    items = change_cards(cs, ctx.tz, ctx.now, ctx.persons, ctx.self_id)
    for item in items:
        ctx.note_candidate(item.key, "change_feed", item.score)
    if not items:
        ctx.notes.append(f"No material change was recorded {window.label}.")
    return Retrieved(items, True, window=window, unresolved=who.unresolved)


def day_cards(
    dv: DayView, tz: ZoneInfo, now: datetime.datetime, persons: dict[UUID, people.PersonRef], self_id: UUID
) -> list[PacketItem]:
    out: list[PacketItem] = []
    for n, m in enumerate(dv.meetings):
        out.append(
            cards.meeting_card(
                m, persons, self_id, tz, now, section="state", priority="anchor", score=100.0 - n
            )
        )
    for n, d in enumerate(dv.decisions):
        out.append(replace(cards.decision_card(d, tz, priority="anchor", score=90.0 - n), group="decisions"))
    latest: dict[UUID, work.TimelineEvent] = {}
    for e in dv.events:
        latest[e.entity_id] = e
    for n, (item_id, e) in enumerate(sorted(latest.items(), key=lambda kv: (-kv[1].materiality, str(kv[0])))):
        view = dv.items.get(item_id)
        if view is None:
            continue
        card = cards.item_card(view, persons, self_id, tz, now, priority="anchor", score=80.0 - n)
        learned = " · learned that day (happened earlier)" if e.id in dv.learned_today else ""
        what = cards.EVENT_TEXT.get(e.event_type, e.event_type.replace("_", " "))
        out.append(
            replace(
                card,
                text=f"[{what}{learned}] {card.text}",
                line=f"{what}: {card.line}{learned}",
                group="items",
            )
        )
    for n, c in enumerate(dv.conversations[:10]):
        out.append(
            replace(cards.conversation_card(c, tz, priority="other_state", score=50.0 - n), group="threads")
        )
    for n, c in enumerate(dv.awaiting[:10]):
        out.append(
            replace(
                cards.conversation_card(c, tz, priority="other_state", score=40.0 - n),
                group="now awaiting your reply",
            )
        )
    return out


async def yesterday(ctx: Ctx) -> Retrieved:
    """S10 chat retriever: the day view of the asked day (default yesterday)."""
    window = ctx.window or temporal.resolve("yesterday", ctx.now, ctx.tz.key)
    assert window is not None
    if window.note:
        ctx.notes.append(window.note)
    dv = await day_view(ctx.uow, start=window.start, end=window.end)
    ids = {
        p
        for v in dv.items.values()
        for p in (v.owner_person_id, v.counterparty_person_id, v.requester_person_id)
        if p
    }
    ids |= {p for m in dv.meetings for p in m.attendee_ids}
    ctx.persons.update(await people.get_persons(ctx.uow, sorted(ids)))
    items = day_cards(dv, ctx.tz, ctx.now, ctx.persons, ctx.self_id)
    for item in items:
        ctx.note_candidate(item.key, "day_view", item.score)
    return Retrieved(items, bool(items), window=window)


# ---------------------------------------------------------------- API services


@dataclass(frozen=True)
class ChangeEntry:
    change: NetChange
    card: PacketItem


@dataclass(frozen=True)
class ChangeFeedPage:
    anchor: TimeWindow
    coverage: Coverage
    entries: list[ChangeEntry]
    next_key: list[object] | None


async def _coverage(
    uow: UnitOfWork, now: datetime.datetime, window: TimeWindow | None
) -> tuple[Coverage, ZoneInfo, UUID]:
    settings = await identity.get_user_settings(uow)
    tz = temporal.zone(settings.timezone)
    states = await connections.sync_states(uow)
    self_p = await people.get_self_person(uow)
    return (
        build_coverage(states, now=now, tz=tz, work_hours=settings.work_hours, window=window),
        tz,
        self_p.id,
    )


async def change_feed(
    uow: UnitOfWork,
    *,
    scope: str,
    since: datetime.datetime | None,
    now: datetime.datetime,
    after: list[object] | None,
    limit: int,
) -> ChangeFeedPage:
    """``GET /api/v1/changes`` (BACKEND_DESIGN.md §16.7)."""
    identity.check_surface(scope)
    person_ids: list[UUID] = []
    if scope.startswith("person:"):
        person_ids = await people.merged_ids(uow, UUID(scope.split(":", 1)[1]))
    explicit = TimeWindow(since, now, "since the given time", "explicit") if since is not None else None
    window = await change_anchor(uow, now=now, surfaces=(scope,), explicit=explicit)
    cs = await changes_since(uow, anchor=window.start, now=now, person_ids=person_ids)
    coverage, tz, self_id = await _coverage(uow, now, window)
    persons = await _persons_for(uow, cs)
    cards_by_key = {c.key: c for c in change_cards(cs, tz, now, persons, self_id, limit=None)}
    entries = []
    for ch in cs.changes:
        card = cards_by_key.get(f"{ch.entity_type}:{ch.entity_id}")
        if card is not None:
            entries.append(ChangeEntry(ch, card))
    start = 0
    if after is not None:
        rank, last_id = float(str(after[0])), str(after[1])
        for n, e in enumerate(entries):
            if (-e.change.rank, str(e.change.entity_id)) > (-rank, last_id):
                start = n
                break
        else:
            start = len(entries)
    page = entries[start : start + limit]
    more = start + limit < len(entries)
    next_key = [page[-1].change.rank, str(page[-1].change.entity_id)] if more and page else None
    return ChangeFeedPage(window, coverage, page, next_key)


@dataclass(frozen=True)
class DayViewPage:
    window: TimeWindow
    coverage: Coverage
    view: DayView
    cards: list[PacketItem]


async def day_view_for(uow: UnitOfWork, *, day: datetime.date, now: datetime.datetime) -> DayViewPage:
    """``GET /api/v1/days/{date}`` (BACKEND_DESIGN.md §16.7): a local calendar day of the user."""
    settings = await identity.get_user_settings(uow)
    window = temporal.day_window(day, settings.timezone)
    coverage, tz, self_id = await _coverage(uow, now, window)
    dv = await day_view(uow, start=window.start, end=window.end)
    ids = {
        p
        for v in dv.items.values()
        for p in (v.owner_person_id, v.counterparty_person_id, v.requester_person_id)
        if p
    }
    ids |= {p for m in dv.meetings for p in m.attendee_ids}
    persons = await people.get_persons(uow, sorted(ids))
    return DayViewPage(window, coverage, dv, day_cards(dv, tz, now, persons, self_id))
