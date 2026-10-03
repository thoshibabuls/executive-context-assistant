"""Normalize messages, reply state and the triage projection (BACKEND_DESIGN.md §9.1, §10.1).

``normalize_source_item`` is idempotent: it acts only on a source item in stage ``fetched`` and
the message row is unique per source item. It resolves participants through ``people``, keeps a
duplicate RFC 822 message (same email under another provider ID) as an alias that stops at
``normalized``, applies the rules prefilter, recomputes the conversation's reply state from its
messages (order-independent, so shuffled replays converge, RT-04), and moves the stage to
``skipped`` or ``extract_pending`` (publishing ``MessageNormalized`` for the latter).
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import any_, func, select, update
from sqlalchemy.dialects.postgresql import insert

from eca.communication.cleaning import clean_body, snippet
from eca.communication.events import MESSAGE_NORMALIZED, MessageNormalized
from eca.communication.models import conversations_table, message_participants_table, messages_table
from eca.communication.prefilter import PrefilterInput, decide
from eca.ingestion import SourceItem, get_source_item, set_stage
from eca.people import (
    MentionIn,
    PersonRef,
    get_persons,
    get_self_person,
    record_interaction,
    record_mentions,
    resolve_address,
)
from eca.platform.events import NewEvent
from eca.platform.ids import uuid7
from eca.platform.outbox import publish
from eca.platform.uow import UnitOfWork

log = structlog.get_logger("eca.communication")

_REQUEST_PATTERN = re.compile(r"\?|\b(could you|can you|would you|please|let me know|need you to)\b", re.I)


@dataclass(frozen=True)
class NormalizeResult:
    message_id: UUID | None
    stage: str
    reason: str | None = None


@dataclass(frozen=True)
class MessageView:
    """What extraction and apply need about a message (read-only DTO; §5.6)."""

    message_id: UUID
    source_item_id: UUID
    conversation_id: UUID
    thread_external_id: str
    subject: str | None
    body_clean: str
    sent_at: datetime.datetime
    direction: str
    sender: PersonRef
    to: tuple[PersonRef, ...]
    cc: tuple[PersonRef, ...]
    is_bulk: bool


def _content(item: SourceItem) -> dict[str, Any]:
    if item.content is None:
        raise ValueError(f"source item {item.id} has no content")
    return item.content


async def _conversation_id(uow: UnitOfWork, item: SourceItem, subject: str | None) -> UUID:
    c = conversations_table
    thread = item.external_thread_id or item.external_id
    await uow.session.execute(
        insert(c)
        .values(
            id=uuid7(),
            user_id=uow.user_id,
            connection_id=item.connection_id,
            kind="email_thread",
            external_thread_id=thread,
            subject=subject,
            awaiting="none",
            needs_reply=False,
            version=1,
        )
        .on_conflict_do_nothing(constraint="ux_conversations_thread")
    )
    row = (
        await uow.session.execute(
            select(c.c.id).where(
                c.c.user_id == uow.user_id,
                c.c.connection_id == item.connection_id,
                c.c.kind == "email_thread",
                c.c.external_thread_id == thread,
            )
        )
    ).one()
    return UUID(str(row.id))


async def recompute_reply_state(uow: UnitOfWork, conversation_id: UUID) -> None:
    """Reply state from all of the conversation's messages (deterministic, order-independent).

    ``awaiting`` follows the latest non-bulk message. ``needs_reply`` comes from the latest
    inbound message's triage when it has one, else from the deterministic heuristic (labelled
    ``heuristic``, AI_PIPELINE.md §4.2 O1). A user's "handled" mark is never overridden.
    """
    m = messages_table
    rows = (
        await uow.session.execute(
            select(m.c.id, m.c.direction, m.c.sent_at, m.c.is_bulk, m.c.body_clean, m.c.triage, m.c.subject)
            .where(m.c.conversation_id == conversation_id, m.c.deleted_at.is_(None))
            .order_by(m.c.sent_at, m.c.id)
        )
    ).all()
    if not rows:
        return
    relevant = [r for r in rows if not r.is_bulk] or rows
    latest = relevant[-1]
    inbound = [r for r in rows if r.direction == "inbound"]
    outbound = [r for r in rows if r.direction == "outbound"]
    awaiting = "user" if latest.direction == "inbound" else "other"
    needs_reply, source = False, "heuristic"
    if awaiting == "user":
        if latest.triage is not None and "needs_reply" in latest.triage:
            needs_reply, source = bool(latest.triage["needs_reply"]), "triage"
        else:
            needs_reply = not latest.is_bulk and bool(_REQUEST_PATTERN.search(latest.body_clean or ""))
    c = conversations_table
    await uow.session.execute(
        update(c)
        .where(c.c.id == conversation_id)
        .values(
            subject=rows[0].subject,  # earliest message: independent of processing order
            first_message_at=rows[0].sent_at,
            last_message_at=rows[-1].sent_at,
            last_inbound_at=max((r.sent_at for r in inbound), default=None),
            last_outbound_at=max((r.sent_at for r in outbound), default=None),
            awaiting=awaiting,
        )
    )
    await uow.session.execute(
        update(c)
        .where(c.c.id == conversation_id, c.c.handled_by_user_at.is_(None))
        .where((c.c.needs_reply_source.is_(None)) | (c.c.needs_reply_source != "user"))
        .values(needs_reply=needs_reply, needs_reply_source=source)
    )


async def _participants(
    uow: UnitOfWork, content: dict[str, Any], seen_at: datetime.datetime
) -> tuple[PersonRef, list[tuple[PersonRef, str]]]:
    sender_raw = content["sender"]
    sender = await resolve_address(
        uow, email=sender_raw["email"], display_name=sender_raw.get("display_name"), seen_at=seen_at
    )
    people: list[tuple[PersonRef, str]] = [(sender, "from")]
    for role in ("to", "cc"):
        for p in content.get(role) or []:
            ref = await resolve_address(
                uow, email=p["email"], display_name=p.get("display_name"), seen_at=seen_at
            )
            people.append((ref, role))
    return sender, people


async def normalize_source_item(uow: UnitOfWork, source_item_id: UUID) -> NormalizeResult:
    item = await get_source_item(uow, source_item_id, for_update=True)
    if item.stage != "fetched":
        return NormalizeResult(message_id=None, stage=item.stage, reason="not_fetched")
    if item.kind != "message":
        return NormalizeResult(message_id=None, stage=item.stage, reason="not_a_message")  # meetings owns it
    content = _content(item)
    m = messages_table
    rfc822 = content.get("rfc822_id")
    if rfc822:
        existing = (
            await uow.session.execute(
                select(m.c.id, m.c.source_item_id).where(
                    m.c.user_id == uow.user_id, m.c.rfc822_message_id == rfc822
                )
            )
        ).one_or_none()
        if existing is not None and existing.source_item_id != item.id:
            await uow.session.execute(
                update(m)
                .where(m.c.id == existing.id)
                .where(~(any_(m.c.alias_source_item_ids) == item.id))
                .values(alias_source_item_ids=func.array_append(m.c.alias_source_item_ids, item.id))
            )
            await set_stage(uow, item.id, expected=("fetched",), new="normalized")
            return NormalizeResult(message_id=existing.id, stage="normalized", reason="duplicate_rfc822")

    self_person = await get_self_person(uow)
    sent_at = datetime.datetime.fromisoformat(content["sent_at"])
    sender, participants = await _participants(uow, content, sent_at)
    outbound = sender.id == self_person.id
    recipients = [p for p, role in participants if role != "from"]
    subject = content.get("subject")
    body_clean = clean_body(content.get("body_text"), content.get("body_html"))
    decision = decide(
        PrefilterInput(
            outbound=outbound,
            sender_email=content["sender"]["email"],
            categories=item.categories,
            headers=content.get("headers_subset") or {},
            subject=subject,
            sender_is_vip=False,
            self_copy=outbound and all(p.id == self_person.id for p in recipients) and bool(recipients),
        )
    )
    conversation_id = await _conversation_id(uow, item, subject)
    message_id = uuid7()
    await uow.session.execute(
        insert(m).values(
            id=message_id,
            user_id=uow.user_id,
            source_item_id=item.id,
            conversation_id=conversation_id,
            rfc822_message_id=rfc822,
            in_reply_to=content.get("in_reply_to"),
            sender_person_id=sender.id,
            direction="outbound" if outbound else "inbound",
            sent_at=sent_at,
            subject=subject,
            body_text=content.get("body_text"),
            body_clean=body_clean,
            snippet=snippet(body_clean),
            is_bulk=decision.is_bulk,
            prefilter_reason=decision.reason,
            alias_source_item_ids=[],
        )
    )
    seen: set[tuple[UUID, str]] = set()
    for person, role in participants:
        if (person.id, role) in seen:
            continue
        seen.add((person.id, role))
        await uow.session.execute(
            insert(message_participants_table)
            .values(message_id=message_id, person_id=person.id, role=role, user_id=uow.user_id)
            .on_conflict_do_nothing()
        )
    for person in {p.id: p for p, _ in participants}.values():
        if person.is_self:
            continue
        await record_interaction(
            uow, person.id, at=sent_at, inbound=(person.id == sender.id) if not outbound else False
        )
    await record_mentions(
        uow,
        [
            MentionIn(
                item.id,
                "person",
                person.id,
                (person.display_name or person.primary_email or ""),
                1.0,
                "header",
                sent_at,
            )
            for person in {p.id: p for p, _ in participants}.values()
            if not person.is_self
        ],
    )
    await recompute_reply_state(uow, conversation_id)

    if decision.skip:
        await set_stage(uow, item.id, expected=("fetched",), new="skipped", error_code=decision.reason)
        return NormalizeResult(message_id=message_id, stage="skipped", reason=decision.reason)
    await set_stage(uow, item.id, expected=("fetched",), new="extract_pending")
    await publish(
        uow,
        NewEvent(
            event_type=MESSAGE_NORMALIZED,
            aggregate_type="message",
            aggregate_id=message_id,
            payload=MessageNormalized(message_id=message_id, source_item_id=item.id),
        ),
    )
    return NormalizeResult(message_id=message_id, stage="extract_pending")


async def get_message_view(uow: UnitOfWork, source_item_id: UUID) -> MessageView:
    m, c = messages_table, conversations_table
    row = (
        await uow.session.execute(
            select(
                m.c.id,
                m.c.source_item_id,
                m.c.conversation_id,
                m.c.subject,
                m.c.body_clean,
                m.c.sent_at,
                m.c.direction,
                m.c.sender_person_id,
                m.c.is_bulk,
                c.c.external_thread_id,
            )
            .join(c, c.c.id == m.c.conversation_id)
            .where(m.c.source_item_id == source_item_id)
        )
    ).one()
    parts = (
        await uow.session.execute(
            select(message_participants_table.c.person_id, message_participants_table.c.role)
            .where(message_participants_table.c.message_id == row.id)
            .order_by(message_participants_table.c.role, message_participants_table.c.person_id)
        )
    ).all()
    persons = await get_persons(uow, [row.sender_person_id, *[p.person_id for p in parts]])
    return MessageView(
        message_id=row.id,
        source_item_id=row.source_item_id,
        conversation_id=row.conversation_id,
        thread_external_id=row.external_thread_id,
        subject=row.subject,
        body_clean=row.body_clean or "",
        sent_at=row.sent_at,
        direction=row.direction,
        sender=persons[row.sender_person_id],
        to=tuple(persons[p.person_id] for p in parts if p.role == "to"),
        cc=tuple(persons[p.person_id] for p in parts if p.role == "cc"),
        is_bulk=row.is_bulk,
    )


async def conversation_source_items(uow: UnitOfWork, conversation_id: UUID) -> list[UUID]:
    m = messages_table
    rows = await uow.session.execute(
        select(m.c.source_item_id).where(m.c.conversation_id == conversation_id).order_by(m.c.sent_at, m.c.id)
    )
    return [r.source_item_id for r in rows]


async def participants_of_conversation(uow: UnitOfWork, conversation_id: UUID) -> list[UUID]:
    mp, m = message_participants_table, messages_table
    rows = await uow.session.execute(
        select(mp.c.person_id)
        .join(m, m.c.id == mp.c.message_id)
        .where(m.c.conversation_id == conversation_id)
        .distinct()
    )
    return sorted(r.person_id for r in rows)


async def set_triage_projection(
    uow: UnitOfWork, *, message_id: UUID, triage: dict[str, Any], extraction_id: UUID
) -> None:
    """``messages.triage`` (AI-derived projection, §6.2); then refresh the reply state."""
    m = messages_table
    row = (
        await uow.session.execute(
            update(m)
            .where(m.c.id == message_id)
            .values(triage=triage, triage_extraction_id=extraction_id)
            .returning(m.c.conversation_id)
        )
    ).one()
    await recompute_reply_state(uow, row.conversation_id)


async def mark_conversation_handled(uow: UnitOfWork, conversation_id: UUID, *, at: datetime.datetime) -> None:
    c = conversations_table
    await uow.session.execute(
        update(c)
        .where(c.c.id == conversation_id)
        .values(handled_by_user_at=at, needs_reply=False, needs_reply_source="user", version=c.c.version + 1)
    )
