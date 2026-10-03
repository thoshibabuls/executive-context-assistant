"""Communication handlers: normalize on ``SourceItemStored`` and on reconciler re-publication."""

from __future__ import annotations

import datetime

from eca.communication.purge import on_source_deleted
from eca.communication.service import normalize_source_item, on_bin_change
from eca.ingestion import (
    SOURCE_ITEM_DELETED,
    SOURCE_ITEM_RESTORED,
    SOURCE_ITEM_STAGE_DUE,
    SOURCE_ITEM_STORED,
    SOURCE_ITEM_TRASHED,
    SourceItemDeleted,
    SourceItemRestored,
    SourceItemStageDue,
    SourceItemStored,
    SourceItemTrashed,
)
from eca.platform.events import HandlerContext, handles

NORMALIZE_HANDLER = "communication.normalize"
NORMALIZE_RETRY_HANDLER = "communication.normalize_due"


@handles(SOURCE_ITEM_STORED, name=NORMALIZE_HANDLER, queue="ingest")
async def on_source_item_stored(ctx: HandlerContext) -> None:
    payload = ctx.payload
    assert isinstance(payload, SourceItemStored)
    await normalize_source_item(ctx.tx, payload.source_item_id)


@handles(SOURCE_ITEM_STAGE_DUE, name=NORMALIZE_RETRY_HANDLER, queue="ingest")
async def on_stage_due(ctx: HandlerContext) -> None:
    payload = ctx.payload
    assert isinstance(payload, SourceItemStageDue)
    if payload.stage == "fetched":
        await normalize_source_item(ctx.tx, payload.source_item_id)


@handles(SOURCE_ITEM_DELETED, name="communication.source_deleted", queue="ingest")
async def on_deleted(ctx: HandlerContext) -> None:
    payload = ctx.payload
    assert isinstance(payload, SourceItemDeleted)
    if payload.kind == "message":
        await on_source_deleted(ctx.tx, payload.source_item_id, now=datetime.datetime.now(datetime.UTC))


@handles(SOURCE_ITEM_TRASHED, name="communication.source_trashed", queue="ingest")
async def on_trashed(ctx: HandlerContext) -> None:
    payload = ctx.payload
    assert isinstance(payload, SourceItemTrashed)
    if payload.kind == "message":
        await on_bin_change(ctx.tx, payload.source_item_id, trashed=True)


@handles(SOURCE_ITEM_RESTORED, name="communication.source_restored", queue="ingest")
async def on_restored(ctx: HandlerContext) -> None:
    payload = ctx.payload
    assert isinstance(payload, SourceItemRestored)
    if payload.kind == "message":
        await on_bin_change(ctx.tx, payload.source_item_id, trashed=False)
