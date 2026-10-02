"""Priority recompute (slice 1.8): reads inputs through the owning modules, writes scores back
through their version-checked services (BACKEND_DESIGN.md §5.1: attention owns no priority
column). Deterministic; no AI call. Unchanged scores are not rewritten.
"""

from __future__ import annotations

import datetime
from uuid import UUID

import structlog

from eca import communication, meetings, people, work
from eca.attention.priority import PriorityConfig, conversation_features, item_features, score
from eca.identity import list_active_user_ids
from eca.platform.uow import UnitOfWork, UnitOfWorkFactory

log = structlog.get_logger("eca.attention")

MEETING_HORIZON = datetime.timedelta(hours=24)


async def recompute_items(
    uow: UnitOfWork, cfg: PriorityConfig, *, now: datetime.datetime, item_ids: list[UUID] | None = None
) -> int:
    items = await work.priority_candidates(uow, item_ids)
    if not items:
        return 0
    person_ids = sorted(
        {
            p
            for i in items
            for p in (i.owner_person_id, i.counterparty_person_id, i.requester_person_id)
            if p is not None
        }
    )
    persons = await people.importance_of(uow, person_ids)
    soon = await meetings.upcoming_attendee_ids(uow, now, MEETING_HORIZON)
    written = 0
    for i in items:
        involved = [p for p in (i.counterparty_person_id, i.requester_person_id) if p is not None]
        features, unknown = item_features(
            cfg,
            direction=i.direction,
            statement_kind=i.statement_kind,
            due_at=i.due_at,
            people=[persons.get(p) for p in involved],
            meeting_soon=any(p in soon for p in involved),
            now=now,
        )
        capped = unknown and bool(involved) and i.verification_status == "suggested"
        value, reasons = score(
            cfg,
            features,
            confidence=None if i.origin == "user" else i.confidence,
            override=i.priority_override,
            capped=capped,
        )
        if i.priority_score is not None and abs(i.priority_score - value) < 0.01:
            continue
        if await work.set_item_priority(uow, i.id, score=value, reasons=reasons, version=i.version, now=now):
            written += 1
    return written


async def recompute_conversations(
    uow: UnitOfWork,
    cfg: PriorityConfig,
    *,
    now: datetime.datetime,
    conversation_ids: list[UUID] | None = None,
) -> int:
    convs = await communication.priority_inputs(uow, conversation_ids)
    if not convs:
        return 0
    persons = await people.importance_of(uow, sorted({p for c in convs for p in c.participant_ids}))
    soon = await meetings.upcoming_attendee_ids(uow, now, MEETING_HORIZON)
    written = 0
    for c in convs:
        features, unknown = conversation_features(
            cfg,
            request_type=c.triage_request_type,
            business_impact=c.triage_business_impact,
            urgency_signals=c.triage_urgency_signals,
            last_inbound_at=c.last_inbound_at,
            people=[persons.get(p) for p in c.participant_ids],
            meeting_soon=any(p in soon for p in c.participant_ids),
            now=now,
        )
        value, reasons = score(
            cfg,
            features,
            confidence=c.triage_confidence,
            override=c.priority_override,
            capped=unknown or c.is_bulk,
        )
        if await communication.set_conversation_priority(
            uow, c.id, score=value, reasons=reasons, version=c.version
        ):
            written += 1
    return written


async def sweep_user(uow: UnitOfWork, cfg: PriorityConfig, *, now: datetime.datetime) -> int:
    await work.clear_closed_priority(uow)
    return await recompute_items(uow, cfg, now=now) + await recompute_conversations(uow, cfg, now=now)


async def sweep_all(factory: UnitOfWorkFactory, cfg: PriorityConfig, *, now: datetime.datetime) -> int:
    async with factory(user_id=None) as uow:
        users = await list_active_user_ids(uow)
    total = 0
    for user_id in users:
        async with factory(user_id=user_id) as uow:
            total += await sweep_user(uow, cfg, now=now)
    log.info("priority_sweep", users=len(users), written=total)
    return total
