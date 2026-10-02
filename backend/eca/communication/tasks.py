"""Communication handlers: normalize on ``SourceItemStored`` and on reconciler re-publication."""

from __future__ import annotations

from eca.communication.service import normalize_source_item
from eca.ingestion import SOURCE_ITEM_STAGE_DUE, SOURCE_ITEM_STORED, SourceItemStageDue, SourceItemStored
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
