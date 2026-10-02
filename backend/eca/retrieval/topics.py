"""S3 project context and topic mode (CONTEXT_ARCHITECTURE.md §10.3, A5; §11.8).

A question about a project first looks for a confirmed project by name or alias (pg_trgm). If
one matches: its card and members, items and decisions linked by assignment or hint match, the
change feed of 14 days, linked threads and meetings of 30 days, and supporting quotes from its
linked sources only. Otherwise **topic mode**: hybrid search on the topic over items, decisions
and chunks of 60 days, grouped by thread or meeting, the top 3 groups expanded relationally and
labelled "grouped by topic; not a confirmed project".
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field, replace
from uuid import UUID
from zoneinfo import ZoneInfo

from eca import communication, meetings, people, projects, work
from eca.platform.uow import UnitOfWork
from eca.projects import ProjectView
from eca.retrieval import cards
from eca.retrieval.changes import changes_since
from eca.retrieval.feed import change_cards
from eca.retrieval.packet import PacketItem
from eca.retrieval.plan import FocusEntry
from eca.retrieval.retrievers import Ctx, Retrieved, _conversation_cards, _discover, _item_cards, _quote_cards
from eca.retrieval.search import ChunkHit, SearchFilters, hybrid_search, ts_terms
from eca.retrieval.temporal import TimeWindow

PROJECT_CHANGES_DAYS = 14
PROJECT_LINKED_DAYS = 30
TOPIC_DAYS = 60
TOPIC_GROUPS = 3
TOPIC_LABEL = "Grouped by topic; not a confirmed project. You can create a project from it."


def project_card(
    p: ProjectView, members: list[str], status: str, last_activity: datetime.datetime | None, tz: ZoneInfo
) -> PacketItem:
    label = (
        "confirmed by you"
        if p.verification_status == "confirmed"
        else ("created by you" if p.origin == "user" else "AI suggestion")
    )
    parts = [f"[project · {label}] {cards.delimit(p.name)}"]
    if p.aliases:
        parts.append("also called " + cards.delimit(", ".join(p.aliases)))
    if members:
        parts.append("members " + cards.delimit(", ".join(members)))
    activity = f"last activity {cards.fmt_date(last_activity, tz)}" if last_activity else "no linked activity"
    parts.append(f"{activity} ({status})")
    return PacketItem(
        key=f"project:{p.id}",
        kind="project",
        section="anchors",
        priority="anchor",
        text=" · ".join(parts),
        line=f"{p.name} — {activity} ({status})",
        data_class="user" if p.confirmed else "ai_derived",
        claim_kind="user" if p.confirmed else "inference",
        authority=5 if p.confirmed else 2,
        entity_id=p.id,
        score=100.0,
        user_backed=p.confirmed,
    )


@dataclass
class ProjectContext:
    project: ProjectView
    items: list[PacketItem]
    status: str
    last_activity: datetime.datetime | None
    sources: list[UUID] = field(default_factory=list)


async def build_project_context(ctx: Ctx, project: ProjectView) -> ProjectContext:
    uow = ctx.uow
    linked_items = await work.items_for_project(
        uow, project_id=project.id, names=project.names, include_closed=True
    )
    open_items = [i for i in linked_items if i.lifecycle_status in ("open", "in_progress")]
    decisions = await work.decisions_for_hints(uow, project.names)
    member_rows = await projects.members_of(uow, project.id)
    await ctx.load_persons([m.person_id for m in member_rows])
    member_names = [cards.person_label(m.person_id, ctx.persons, ctx.self_id) for m in member_rows]
    since = ctx.now - datetime.timedelta(days=PROJECT_LINKED_DAYS)
    sources = sorted({s for i in linked_items for s in i.evidence_source_ids})
    mentioned = await people.mentions_of(uow, "project", [project.id], since=since)
    sources = sorted(set(sources) | {m.source_item_id for m in mentioned})
    conv_of = await communication.conversation_ids_of_sources(uow, sources)
    threads = [
        c
        for c in await communication.conversations_by_ids(uow, sorted(set(conv_of.values())))
        if c.last_message_at is None or c.last_message_at >= since
    ]
    linked_meetings = await meetings.meetings_by_sources(uow, sources)
    last = max(
        [i.last_activity_at for i in linked_items if i.last_activity_at]
        + [c.last_message_at for c in threads if c.last_message_at],
        default=None,
    )
    status = projects.activity_status(last, ctx.now)
    items: list[PacketItem] = [project_card(project, member_names, status, last, ctx.tz)]
    if status == "quiet":
        ctx.notes.append(
            f"No linked activity since {cards.fmt_date(last, ctx.tz)}."
            if last
            else "No linked activity was found."
        )
    items += await _item_cards(ctx, open_items[:20], priority="anchor")
    items += await _quote_cards(ctx, "work_item", [i.id for i in open_items[:8]], per_item=2)
    for d in decisions:
        items.append(cards.decision_card(d, ctx.tz))
    cs = await changes_since(
        uow,
        anchor=ctx.now - datetime.timedelta(days=PROJECT_CHANGES_DAYS),
        now=ctx.now,
        item_filter={i.id for i in linked_items},
    )
    cs_items = [
        c for c in cs.changes if c.entity_type == "work_item" and c.entity_id in {i.id for i in linked_items}
    ]
    narrowed = replace(cs, changes=cs_items)
    items += [
        replace(c, priority="anchor_timeline", section="timeline")
        for c in change_cards(narrowed, ctx.tz, ctx.now, ctx.persons, ctx.self_id)
    ]
    items += await _conversation_cards(ctx, threads[:10])
    await ctx.load_persons([p for m in linked_meetings for p in m.attendee_ids])
    for m in linked_meetings[-5:]:
        items.append(
            cards.meeting_card(
                m, ctx.persons, ctx.self_id, ctx.tz, ctx.now, section="state", priority="other_state"
            )
        )
    if sources:
        hits = await hybrid_search(
            uow,
            terms=ts_terms(f"{project.name} {ctx.plan.topic or ''}"),
            query_vector=ctx.query_vector,
            embedding_model=ctx.embedding_model,
            filters=SearchFilters(
                allowed_sources=tuple(sources),
                exclude_calendar_connections=ctx.filters.exclude_calendar_connections,
            ),
            limit=8,
        )
        items += [cards.chunk_card(h, ctx.tz, score=h.rrf) for h in hits]
    ctx.focus.append(FocusEntry("project", project.id, project.name, 0))
    return ProjectContext(project, items, status, last, sources)


@dataclass
class TopicGroup:
    kind: str  # conversation | meeting | other
    id: UUID | None
    score: float
    hits: list[ChunkHit] = field(default_factory=list)
    item_ids: set[UUID] = field(default_factory=set)
    decision_ids: set[UUID] = field(default_factory=set)
    sources: set[UUID] = field(default_factory=set)


async def topic_groups(ctx: Ctx, topic: str) -> list[TopicGroup]:
    """Top groups (thread or meeting) for a topic over the last 60 days (§10.3 topic mode)."""
    since = ctx.now - datetime.timedelta(days=TOPIC_DAYS)
    terms = ts_terms(topic)
    hits = await _discover(ctx, topic, since=since)
    found_items = await work.search_items(ctx.uow, terms, limit=20, include_closed=True)
    found_decisions = await work.search_decisions(ctx.uow, terms, limit=10)
    groups: dict[tuple[str, UUID | None], TopicGroup] = {}

    def group(kind: str, gid: UUID | None) -> TopicGroup:
        key = (kind, gid)
        if key not in groups:
            groups[key] = TopicGroup(kind, gid, 0.0)
        return groups[key]

    for h in hits:
        if h.conversation_id is not None:
            g = group("conversation", h.conversation_id)
        elif h.meeting_id is not None:
            g = group("meeting", h.meeting_id)
        else:
            g = group("other", None)
        g.score += h.rrf
        g.hits.append(h)
        g.sources.add(h.source_item_id)
    item_sources = {i.id: i.evidence_source_ids for i, _ in found_items}
    all_sources = sorted({s for srcs in item_sources.values() for s in srcs})
    conv_of = await communication.conversation_ids_of_sources(ctx.uow, all_sources)
    meeting_of = {m.source_item_id: m.id for m in await meetings.meetings_by_sources(ctx.uow, all_sources)}
    for item, rank in found_items:
        placed = False
        for s in item.evidence_source_ids:
            if s in conv_of:
                g = group("conversation", conv_of[s])
            elif s in meeting_of:
                g = group("meeting", meeting_of[s])
            else:
                continue
            g.score += rank / 10.0
            g.item_ids.add(item.id)
            g.sources.add(s)
            placed = True
        if not placed:
            group("other", None).item_ids.add(item.id)
    for d, rank in found_decisions:
        if d.conversation_id is not None:
            g = group("conversation", d.conversation_id)
            g.score += rank / 10.0
            g.decision_ids.add(d.id)
    ranked = sorted(
        (g for g in groups.values() if g.kind != "other" and (g.hits or g.item_ids or g.decision_ids)),
        key=lambda g: (-g.score, str(g.id)),
    )
    return ranked[:TOPIC_GROUPS]


async def topic_mode(ctx: Ctx) -> Retrieved:
    topic = ctx.plan.topic or ctx.plan.qualifier or ""
    groups = await topic_groups(ctx, topic)
    window = TimeWindow(
        ctx.now - datetime.timedelta(days=TOPIC_DAYS), ctx.now, f"the last {TOPIC_DAYS} days", "default"
    )
    if not groups:
        return Retrieved([], False, window=window)
    ctx.notes.append(TOPIC_LABEL)
    items: list[PacketItem] = []
    for n, g in enumerate(groups):
        label = f"topic group {n + 1}"
        more_items, more_decisions = await work.item_ids_for_sources(ctx.uow, sorted(g.sources))
        item_ids = sorted(g.item_ids | set(more_items))
        views = await work.items_by_ids(ctx.uow, item_ids)
        cards_ = await _item_cards(ctx, views, priority="anchor")
        if g.kind == "conversation" and g.id is not None:
            cards_ += await _conversation_cards(
                ctx, await communication.conversations_by_ids(ctx.uow, [g.id]), priority="anchor"
            )
        elif g.kind == "meeting" and g.id is not None:
            found = await meetings.get_meeting_details(ctx.uow, [g.id])
            await ctx.load_persons([p for m in found for p in m.attendee_ids])
            cards_ += [cards.meeting_card(m, ctx.persons, ctx.self_id, ctx.tz, ctx.now) for m in found]
        for d in await work.decisions_by_ids(ctx.uow, sorted(g.decision_ids | set(more_decisions))):
            cards_.append(cards.decision_card(d, ctx.tz))
        cards_ += [cards.chunk_card(h, ctx.tz, score=h.rrf) for h in g.hits[:3]]
        items += [replace(c, group=label, score=c.score + (TOPIC_GROUPS - n) * 1000) for c in cards_]
    matched = any(h.is_match for g in groups for h in g.hits) or any(
        g.item_ids or g.decision_ids for g in groups
    )
    return Retrieved(items, matched, window=window)


async def project_or_topic(ctx: Ctx) -> Retrieved:
    """S3: the confirmed project the question names, else topic mode."""
    topic = ctx.plan.topic or ctx.plan.qualifier or ""
    matches = await projects.match_projects(ctx.uow, topic) if topic else []
    if not matches:
        return await topic_mode(ctx)
    context = await build_project_context(ctx, matches[0][0])
    return Retrieved(context.items, True)


# ---------------------------------------------------------------- API services


@dataclass(frozen=True)
class ProjectContextPage:
    project: ProjectView
    status: str
    last_activity: datetime.datetime | None
    cards: list[PacketItem]


async def project_context_for(ctx: Ctx, project_id: UUID) -> ProjectContextPage:
    project = await projects.get_project(ctx.uow, project_id)
    built = await build_project_context(ctx, project)
    return ProjectContextPage(project, built.status, built.last_activity, built.items)


async def project_item_ids(uow: UnitOfWork, project_id: UUID) -> tuple[set[UUID], set[UUID]]:
    """(item IDs, decision IDs) linked to a project: the change feed's ``project:<id>`` scope."""
    project = await projects.get_project(uow, project_id)
    items = await work.items_for_project(
        uow, project_id=project.id, names=project.names, include_closed=True, limit=500
    )
    decisions = await work.decisions_for_hints(uow, project.names, limit=200)
    return {i.id for i in items}, {d.id for d in decisions}
