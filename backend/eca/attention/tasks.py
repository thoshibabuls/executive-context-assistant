"""Attention handlers and the priority sweep (slice 1.8).

Event reactions go through the outbox (BACKEND_DESIGN.md §5.2: no import cycle with work or
communication): ``WorkItemChanged`` recomputes that item, ``MessageNormalized`` that
conversation, ``MeetingChanged`` the user's open items and conversations (meeting proximity),
``PersonChanged`` (a user correction of importance or identity, Phase 3) the user's items and
conversations. The 15-minute sweep covers time-based features (deadline urgency, activity
windows) and triage written by apply.

Relationship profiles (Phase 3, CONTEXT_ARCHITECTURE.md §5.3): ``PersonChanged`` recomputes that
person, ``MessageNormalized`` the message's participants (at most 10), and a nightly task every
active person of every active user.
"""

from __future__ import annotations

import datetime
from functools import lru_cache

from eca.attention.priority import PriorityConfig
from eca.attention.profiles import refresh_all, refresh_profiles
from eca.attention.service import recompute_conversations, recompute_items, sweep_all, sweep_user
from eca.communication import (
    MESSAGE_NORMALIZED,
    MessageNormalized,
    message_conversation_id,
    message_participant_ids,
)
from eca.meetings import MEETING_CHANGED, MeetingChanged
from eca.people import PERSON_CHANGED, PersonChanged
from eca.platform.clock import Clock
from eca.platform.events import HandlerContext, handles
from eca.platform.jobs import PeriodicTaskSpec
from eca.platform.uow import UnitOfWorkFactory
from eca.work import WORK_ITEM_CHANGED, WorkItemChanged

PRIORITY_SWEEP_TASK = "eca.attention.priority_sweep"
PROFILES_TASK = "eca.attention.relationship_profiles"
PROFILE_PARTICIPANTS = 10


@lru_cache(maxsize=1)
def priority_config() -> PriorityConfig:
    return PriorityConfig.load()


@handles(WORK_ITEM_CHANGED, name="attention.priority_item")
async def on_work_item_changed(ctx: HandlerContext) -> None:
    payload = ctx.payload
    assert isinstance(payload, WorkItemChanged)
    await recompute_items(
        ctx.tx, priority_config(), now=ctx.resources.get(Clock).now(), item_ids=[payload.work_item_id]
    )


@handles(MESSAGE_NORMALIZED, name="attention.priority_conversation")
async def on_message_normalized(ctx: HandlerContext) -> None:
    payload = ctx.payload
    assert isinstance(payload, MessageNormalized)
    conversation_id = await message_conversation_id(ctx.tx, payload.message_id)
    if conversation_id is not None:
        await recompute_conversations(
            ctx.tx, priority_config(), now=ctx.resources.get(Clock).now(), conversation_ids=[conversation_id]
        )


@handles(MEETING_CHANGED, name="attention.priority_meeting")
async def on_meeting_changed(ctx: HandlerContext) -> None:
    assert isinstance(ctx.payload, MeetingChanged)
    await sweep_user(ctx.tx, priority_config(), now=ctx.resources.get(Clock).now())


@handles(PERSON_CHANGED, name="attention.priority_person")
async def on_person_changed(ctx: HandlerContext) -> None:
    """A user correction of a person: their profile first (it feeds the sender feature), then the
    user's priorities (``recompute_priority``, a pure projection)."""
    payload = ctx.payload
    assert isinstance(payload, PersonChanged)
    now = ctx.resources.get(Clock).now()
    ids = [payload.person_id] + ([payload.merged_into_id] if payload.merged_into_id else [])
    await refresh_profiles(ctx.tx, now=now, person_ids=ids)
    await sweep_user(ctx.tx, priority_config(), now=now)


@handles(MESSAGE_NORMALIZED, name="attention.profile_message")
async def on_message_profiles(ctx: HandlerContext) -> None:
    payload = ctx.payload
    assert isinstance(payload, MessageNormalized)
    participants = (await message_participant_ids(ctx.tx, payload.message_id))[:PROFILE_PARTICIPANTS]
    if participants:
        await refresh_profiles(ctx.tx, now=ctx.resources.get(Clock).now(), person_ids=participants)


async def _profiles(uow_factory: UnitOfWorkFactory, now: datetime.datetime) -> None:
    await refresh_all(uow_factory, now=now)


async def _sweep(uow_factory: UnitOfWorkFactory, now: datetime.datetime) -> None:
    await sweep_all(uow_factory, priority_config(), now=now)


def periodic_tasks() -> list[PeriodicTaskSpec]:
    return [
        PeriodicTaskSpec(
            name=PRIORITY_SWEEP_TASK,
            periodic_id="priority_sweep",
            cron="*/15 * * * *",
            queue="schedule",
            run=_sweep,
        ),
        PeriodicTaskSpec(
            name=PROFILES_TASK,
            periodic_id="relationship_profiles",
            cron="20 3 * * *",
            queue="schedule",
            run=_profiles,
        ),
    ]
