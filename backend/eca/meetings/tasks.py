"""Meetings handlers: calendar source items → meetings."""

from __future__ import annotations

from eca.ingestion import (
    SOURCE_ITEM_DELETED,
    SOURCE_ITEM_STAGE_DUE,
    SOURCE_ITEM_STORED,
    SourceItemDeleted,
    SourceItemStageDue,
    SourceItemStored,
)
from eca.meetings.service import cancel_from_deleted_source, upsert_from_source
from eca.platform.events import HandlerContext, handles


@handles(SOURCE_ITEM_STORED, name="meetings.upsert", queue="ingest")
async def on_source_item_stored(ctx: HandlerContext) -> None:
    payload = ctx.payload
    assert isinstance(payload, SourceItemStored)
    if payload.kind == "calendar_event":
        await upsert_from_source(ctx.tx, payload.source_item_id)


@handles(SOURCE_ITEM_STAGE_DUE, name="meetings.upsert_due", queue="ingest")
async def on_stage_due(ctx: HandlerContext) -> None:
    payload = ctx.payload
    assert isinstance(payload, SourceItemStageDue)
    if payload.stage == "fetched":
        await upsert_from_source(ctx.tx, payload.source_item_id)


@handles(SOURCE_ITEM_DELETED, name="meetings.source_deleted", queue="ingest")
async def on_source_deleted(ctx: HandlerContext) -> None:
    payload = ctx.payload
    assert isinstance(payload, SourceItemDeleted)
    if payload.kind == "calendar_event":
        await cancel_from_deleted_source(ctx.tx, payload.source_item_id)
