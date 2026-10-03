"""Reply-guidance packet (scenario RG; AI_PIPELINE.md §4.2 O13, §5.9; CONTEXT_ARCHITECTURE.md §9.6).

Reuses the S1 email-context retriever for the thread and the S2 person-context retriever for the
newest inbound sender (fixed anchor), and adds the thread's AI-03 summary or gist timeline and its
last 3 messages as delimited SOURCE text. No model call; packed within 4,000/8,000 tokens.
"""

from __future__ import annotations

from uuid import UUID

from eca import communication, people
from eca.platform.errors import NotFound
from eca.platform.uow import UnitOfWork
from eca.retrieval import cards
from eca.retrieval.packet import PacketItem
from eca.retrieval.retrievers import Ctx, Retrieved, email_context, person_context

MESSAGE_TOKENS = 400


def _cut(value: str, tokens: int) -> str:
    limit = tokens * 4
    return value if len(value) <= limit else value[: limit - 1] + "…"


async def reply_guidance(ctx: Ctx) -> Retrieved:
    """``ctx.plan.conversation_id`` is the thread; ``ctx.plan.person_ids`` the newest inbound sender."""
    conversation_id = ctx.plan.conversation_id
    if conversation_id is None:
        return Retrieved([], False)
    thread = await email_context(ctx)
    if not thread.matched:
        return Retrieved([], False)
    items: list[PacketItem] = list(thread.items)
    if ctx.plan.person_ids:
        items += (await person_context(ctx)).items
    summary = await communication.thread_summary(ctx.uow, conversation_id)
    if summary.text and not summary.stale:
        items.append(
            PacketItem(
                key=f"summary:{conversation_id}",
                kind="summary",
                section="state",
                priority="other_state",
                text=f"[thread summary · AI-derived, not a source] {cards.delimit(summary.text)}",
                line="AI summary of the thread",
                data_class="ai_derived",
                claim_kind="inference",
                authority=1,
                entity_id=conversation_id,
                source_item_ids=summary.covered_source_ids,
                as_of=summary.derived_at,
            )
        )
    else:
        gists = await communication.gist_timeline(ctx.uow, conversation_id)
        if gists:
            await ctx.load_persons([g.sender_person_id for g in gists])
            lines = []
            for g in gists:
                sender = cards.person_label(g.sender_person_id, ctx.persons, ctx.self_id)
                lines.append(
                    f"{cards.fmt_date(g.sent_at, ctx.tz)} {cards.delimit(sender)}: {cards.delimit(g.gist)}"
                )
            items.append(
                PacketItem(
                    key=f"gists:{conversation_id}",
                    kind="gist_timeline",
                    section="state",
                    priority="other_state",
                    text="[gist timeline · AI-derived one-line gists] " + " | ".join(lines),
                    line="Gist timeline of the thread",
                    data_class="ai_derived",
                    claim_kind="inference",
                    authority=1,
                    entity_id=conversation_id,
                    source_item_ids=tuple(g.source_item_id for g in gists),
                )
            )
    messages = await communication.last_messages(ctx.uow, conversation_id, limit=3)
    await ctx.load_persons([m.sender_person_id for m in messages])
    for m in messages:
        sender = cards.person_label(m.sender_person_id, ctx.persons, ctx.self_id)
        kind = "gist; body no longer kept" if m.from_gist else m.direction
        when = cards.fmt_date(m.sent_at, ctx.tz, with_time=True)
        items.append(
            PacketItem(
                key=f"message:{m.message_id}",
                kind="message",
                section="timeline",
                priority="anchor_timeline",
                text=f"[message · {kind} · from {cards.delimit(sender)} · {when}] "
                f"{cards.delimit(_cut(m.text, MESSAGE_TOKENS))}",
                line=f"Message from {sender} ({cards.fmt_date(m.sent_at, ctx.tz)})",
                data_class="ai_derived" if m.from_gist else "source",
                claim_kind="inference" if m.from_gist else "source",
                authority=1 if m.from_gist else 3,
                entity_id=m.message_id,
                source_item_ids=(m.source_item_id,),
                as_of=m.sent_at,
            )
        )
    return Retrieved(items, True)


async def newest_inbound_sender(uow: UnitOfWork, conversation_id: UUID) -> UUID | None:
    """The person the reply goes to (the S2 anchor), or None for a thread without inbound mail."""
    latest = await communication.latest_messages(uow, [conversation_id])
    sender = latest[conversation_id].sender_person_id if conversation_id in latest else None
    if sender is None:
        return None
    self_id = (await people.get_self_person(uow)).id
    return None if sender == self_id else sender


async def reply_anchor(uow: UnitOfWork, conversation_id: UUID) -> UUID | None:
    """404 for an unknown (or another user's) thread; else the reply's recipient, if any."""
    if not await communication.conversations_by_ids(uow, [conversation_id]):
        raise NotFound("conversation not found")
    return await newest_inbound_sender(uow, conversation_id)
