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

Reminders (slice 3.1, BACKEND_DESIGN.md §15): ``evaluate_reminders`` handlers on
``WorkItemChanged``, ``MessageNormalized`` and ``MeetingChanged`` under the advisory lock
``rem:{entity}``; the 5-minute ``reminder_sweep`` (lock ``reminder_sweep``); the natural-key
``attention.web_push`` handler on ``ReminderDue``.
"""

from __future__ import annotations

import datetime
from functools import lru_cache
from uuid import UUID

from sqlalchemy import text

from eca.attention.briefing import on_briefing_due, schedule_all
from eca.attention.events import BRIEFING_DUE, REMINDER_DUE, BriefingDue, ReminderDue
from eca.attention.priority import PriorityConfig
from eca.attention.profiles import refresh_all, refresh_profiles
from eca.attention.push import send_push
from eca.attention.reminders import evaluate
from eca.attention.reminders import sweep_all as reminder_sweep_all
from eca.attention.service import recompute_conversations, recompute_items, sweep_all, sweep_user
from eca.attention.webpush import WebPushSender
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
from eca.platform.uow import UnitOfWork, UnitOfWorkFactory
from eca.work import WORK_ITEM_CHANGED, WorkItemChanged

PRIORITY_SWEEP_TASK = "eca.attention.priority_sweep"
PROFILES_TASK = "eca.attention.relationship_profiles"
REMINDER_SWEEP_TASK = "eca.attention.reminder_sweep"
BRIEFING_SCHEDULE_TASK = "eca.attention.briefing_schedule"
_REM_LOCK_SQL = text("SELECT pg_advisory_xact_lock(hashtextextended('rem:' || :entity, 0))")
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


async def _rem_lock(uow: UnitOfWork, entity_id: UUID) -> None:
    await uow.session.execute(_REM_LOCK_SQL, {"entity": str(entity_id)})


@handles(WORK_ITEM_CHANGED, name="attention.reminders_item")
async def on_item_reminders(ctx: HandlerContext) -> None:
    payload = ctx.payload
    assert isinstance(payload, WorkItemChanged)
    await _rem_lock(ctx.tx, payload.work_item_id)
    await evaluate(ctx.tx, now=ctx.resources.get(Clock).now(), item_ids=[payload.work_item_id])


@handles(MESSAGE_NORMALIZED, name="attention.reminders_conversation")
async def on_conversation_reminders(ctx: HandlerContext) -> None:
    payload = ctx.payload
    assert isinstance(payload, MessageNormalized)
    conversation_id = await message_conversation_id(ctx.tx, payload.message_id)
    if conversation_id is not None:
        await _rem_lock(ctx.tx, conversation_id)
        await evaluate(ctx.tx, now=ctx.resources.get(Clock).now(), conversation_ids=[conversation_id])


@handles(MEETING_CHANGED, name="attention.reminders_meeting")
async def on_meeting_reminders(ctx: HandlerContext) -> None:
    payload = ctx.payload
    assert isinstance(payload, MeetingChanged)
    await _rem_lock(ctx.tx, payload.meeting_id)
    await evaluate(ctx.tx, now=ctx.resources.get(Clock).now(), meeting_ids=[payload.meeting_id])


@handles(REMINDER_DUE, name="attention.web_push", mode="natural_key")
async def on_reminder_due(ctx: HandlerContext) -> None:
    """The HTTP call to the push service runs outside any transaction (§7.4)."""
    payload = ctx.payload
    assert isinstance(payload, ReminderDue) and ctx.envelope.user_id is not None
    try:
        sender: WebPushSender | None = ctx.resources.get(WebPushSender)
    except LookupError:
        sender = None
    await send_push(
        ctx.factory,
        sender,
        user_id=ctx.envelope.user_id,
        reminder_id=payload.reminder_id,
        seq=payload.seq,
        now=ctx.resources.get(Clock).now(),
    )


@handles(BRIEFING_DUE, name="attention.daily_briefing")
async def on_daily_briefing(ctx: HandlerContext) -> None:
    """``daily_briefing``: deterministic, once per user-day (lock ``brief:{user}:{date}``)."""
    payload = ctx.payload
    assert isinstance(payload, BriefingDue)
    await on_briefing_due(ctx.tx, payload.date, now=ctx.resources.get(Clock).now())


async def _briefings(uow_factory: UnitOfWorkFactory, now: datetime.datetime) -> None:
    await schedule_all(uow_factory, now=now)


async def _reminders(uow_factory: UnitOfWorkFactory, now: datetime.datetime) -> None:
    await reminder_sweep_all(uow_factory, now=now)


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
            name=REMINDER_SWEEP_TASK,
            periodic_id="reminder_sweep",
            cron="*/5 * * * *",
            queue="schedule",
            run=_reminders,
        ),
        PeriodicTaskSpec(
            name=BRIEFING_SCHEDULE_TASK,
            periodic_id="briefing_schedule",
            cron="*/15 * * * *",
            queue="schedule",
            run=_briefings,
        ),
        PeriodicTaskSpec(
            name=PROFILES_TASK,
            periodic_id="relationship_profiles",
            cron="20 3 * * *",
            queue="schedule",
            run=_profiles,
        ),
    ]
