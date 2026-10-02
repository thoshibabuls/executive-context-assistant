"""Retrieval handlers: the index job on ``MessageNormalized`` and ``MeetingChanged``, chunk removal
on ``SourceItemDeleted`` (BACKEND_DESIGN.md §15 Phase 2 jobs). ``TranscriptStored`` joins in
slice 4.2, when transcripts exist."""

from __future__ import annotations

import datetime

from eca.communication import MESSAGE_NORMALIZED, MessageNormalized
from eca.ingestion import SOURCE_ITEM_DELETED, SourceItemDeleted
from eca.intelligence import AIClient
from eca.meetings import MEETING_CHANGED, MeetingChanged
from eca.platform.clock import Clock
from eca.platform.events import HandlerContext, handles
from eca.platform.uow import UnitOfWork
from eca.retrieval.indexing import IndexTarget, index_source, meeting_target, message_target, remove_source

INDEX_MESSAGE_HANDLER = "retrieval.index_message"
INDEX_MEETING_HANDLER = "retrieval.index_meeting"
INDEX_REMOVED_HANDLER = "retrieval.index_removed"


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


@handles(SOURCE_ITEM_DELETED, name=INDEX_REMOVED_HANDLER, queue="embed")
async def on_source_item_deleted(ctx: HandlerContext) -> None:
    payload = ctx.payload
    assert isinstance(payload, SourceItemDeleted)
    await remove_source(ctx.tx, payload.source_item_id)
