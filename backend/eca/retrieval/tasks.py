"""Retrieval handlers: the index job on ``MessageNormalized``, ``MeetingChanged``, (Phase 4)
``TranscriptStored`` and ``SpeakerMappingChanged``, chunk removal on ``SourceItemDeleted``
(BACKEND_DESIGN.md §15). Phase 3: AI-03 on ``ThreadSummaryDue`` (natural key, queue ``extract``)
and the 5-minute ``thread_summary_sweep``. Phase 4: AI-11 suggested asks on
``MeetingAsksRequested`` (natural key, queue ``ai_standard``)."""

from __future__ import annotations

import datetime
from uuid import UUID

from eca.communication import MESSAGE_NORMALIZED, MessageNormalized
from eca.ingestion import SOURCE_ITEM_DELETED, SourceItemDeleted
from eca.intelligence import AIClient
from eca.meetings import (
    MEETING_ASKS_REQUESTED,
    MEETING_CHANGED,
    SPEAKER_MAPPING_CHANGED,
    TRANSCRIPT_STORED,
    MeetingAsksRequested,
    MeetingChanged,
    SpeakerMappingChanged,
    TranscriptStored,
)
from eca.platform.clock import Clock
from eca.platform.events import HandlerContext, handles
from eca.platform.jobs import PeriodicTaskSpec
from eca.platform.uow import UnitOfWork, UnitOfWorkFactory
from eca.retrieval import summaries
from eca.retrieval.asks import generate_asks
from eca.retrieval.events import THREAD_SUMMARY_DUE, ThreadSummaryDue
from eca.retrieval.indexing import (
    IndexTarget,
    index_source,
    meeting_target,
    message_target,
    remove_source,
    transcript_target,
)

INDEX_MESSAGE_HANDLER = "retrieval.index_message"
INDEX_MEETING_HANDLER = "retrieval.index_meeting"
INDEX_REMOVED_HANDLER = "retrieval.index_removed"
INDEX_TRANSCRIPT_HANDLER = "retrieval.index_transcript"
INDEX_SPEAKERS_HANDLER = "retrieval.index_speakers"
THREAD_SUMMARY_HANDLER = "retrieval.thread_summary"
MEETING_ASKS_HANDLER = "retrieval.meeting_asks"
THREAD_SUMMARY_SWEEP_TASK = "eca.retrieval.thread_summary_sweep"


def _now(ctx: HandlerContext) -> datetime.datetime:
    try:
        return ctx.resources.get(Clock).now()
    except LookupError:
        return datetime.datetime.now(datetime.UTC)


@handles(MESSAGE_NORMALIZED, name=INDEX_MESSAGE_HANDLER, queue="embed", mode="natural_key")
async def on_message_normalized(ctx: HandlerContext) -> None:
    payload = ctx.payload
    assert isinstance(payload, MessageNormalized) and ctx.envelope.user_id is not None
    source_item_id = payload.source_item_id

    async def load(uow: UnitOfWork) -> IndexTarget | None:
        return await message_target(uow, source_item_id)

    await index_source(
        ctx.factory,
        ctx.resources.get(AIClient),
        user_id=ctx.envelope.user_id,
        load=load,
        attempt=ctx.attempt,
        now=_now(ctx),
    )


@handles(MEETING_CHANGED, name=INDEX_MEETING_HANDLER, queue="embed", mode="natural_key")
async def on_meeting_changed(ctx: HandlerContext) -> None:
    payload = ctx.payload
    assert isinstance(payload, MeetingChanged) and ctx.envelope.user_id is not None
    meeting_id = payload.meeting_id

    async def load(uow: UnitOfWork) -> IndexTarget | None:
        return await meeting_target(uow, meeting_id)

    await index_source(
        ctx.factory,
        ctx.resources.get(AIClient),
        user_id=ctx.envelope.user_id,
        load=load,
        attempt=ctx.attempt,
        now=_now(ctx),
    )


async def index_recording(ctx: HandlerContext, recording_id: UUID) -> None:
    assert ctx.envelope.user_id is not None

    async def load(uow: UnitOfWork) -> IndexTarget | None:
        return await transcript_target(uow, recording_id)

    await index_source(
        ctx.factory,
        ctx.resources.get(AIClient),
        user_id=ctx.envelope.user_id,
        load=load,
        attempt=ctx.attempt,
        now=_now(ctx),
    )


@handles(TRANSCRIPT_STORED, name=INDEX_TRANSCRIPT_HANDLER, queue="embed", mode="natural_key")
async def on_transcript_stored(ctx: HandlerContext) -> None:
    payload = ctx.payload
    assert isinstance(payload, TranscriptStored)
    await index_recording(ctx, payload.recording_id)


@handles(SPEAKER_MAPPING_CHANGED, name=INDEX_SPEAKERS_HANDLER, queue="embed", mode="natural_key")
async def on_speaker_mapping_changed(ctx: HandlerContext) -> None:
    """Transcript windows carry speaker names: re-index; unchanged windows keep their vectors."""
    payload = ctx.payload
    assert isinstance(payload, SpeakerMappingChanged)
    if payload.recording_id is not None:
        await index_recording(ctx, payload.recording_id)


@handles(SOURCE_ITEM_DELETED, name=INDEX_REMOVED_HANDLER, queue="embed")
async def on_source_item_deleted(ctx: HandlerContext) -> None:
    payload = ctx.payload
    assert isinstance(payload, SourceItemDeleted)
    await remove_source(ctx.tx, payload.source_item_id)


@handles(THREAD_SUMMARY_DUE, name=THREAD_SUMMARY_HANDLER, queue="extract", mode="natural_key")
async def on_thread_summary_due(ctx: HandlerContext) -> None:
    payload = ctx.payload
    assert isinstance(payload, ThreadSummaryDue) and ctx.envelope.user_id is not None
    await summaries.summarize(
        ctx.factory,
        ctx.resources.get(AIClient),
        user_id=ctx.envelope.user_id,
        conversation_id=payload.conversation_id,
        through_message_id=payload.through_message_id,
        now=_now(ctx),
    )


@handles(MEETING_ASKS_REQUESTED, name=MEETING_ASKS_HANDLER, queue="ai_standard", mode="natural_key")
async def on_meeting_asks_requested(ctx: HandlerContext) -> None:
    """AI-11 once per meeting prep version (BACKEND_DESIGN.md §15 Phase 4 jobs)."""
    payload = ctx.payload
    assert isinstance(payload, MeetingAsksRequested) and ctx.envelope.user_id is not None
    await generate_asks(
        ctx.factory,
        ctx.resources.get(AIClient),
        user_id=ctx.envelope.user_id,
        meeting_id=payload.meeting_id,
        version=payload.version,
        now=_now(ctx),
    )


async def _summary_sweep(uow_factory: UnitOfWorkFactory, now: datetime.datetime) -> None:
    await summaries.sweep_all(uow_factory, now=now)


def periodic_tasks() -> list[PeriodicTaskSpec]:
    return [
        PeriodicTaskSpec(
            name=THREAD_SUMMARY_SWEEP_TASK,
            periodic_id="thread_summary_sweep",
            cron="*/5 * * * *",
            queue="schedule",
            run=_summary_sweep,
        )
    ]
