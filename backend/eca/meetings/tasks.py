"""Meetings handlers: calendar source items → meetings; Phase 4: the media sweep (upload expiry,
retry and budget re-publish, raw media retention, expired provider-file references)."""

from __future__ import annotations

import datetime
from functools import lru_cache

import structlog

from eca.identity import list_active_user_ids
from eca.ingestion import (
    SOURCE_ITEM_DELETED,
    SOURCE_ITEM_STAGE_DUE,
    SOURCE_ITEM_STORED,
    SourceItemDeleted,
    SourceItemStageDue,
    SourceItemStored,
)
from eca.meetings.recordings import (
    clear_expired_provider_files,
    expire_pending_uploads,
    publish_due_stages,
    purge_raw_media,
)
from eca.meetings.service import cancel_from_deleted_source, upsert_from_source
from eca.platform.config import get_settings
from eca.platform.events import HandlerContext, handles
from eca.platform.jobs import PeriodicTaskSpec
from eca.platform.storage import ObjectStorage, build_storage
from eca.platform.uow import UnitOfWorkFactory

log = structlog.get_logger("eca.meetings")


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


# ---------------------------------------------------------------- media sweep (Phase 4)

MEDIA_SWEEP_TASK = "eca.meetings.media_sweep"


@lru_cache(maxsize=1)
def _storage() -> ObjectStorage:
    return build_storage(get_settings())


async def _media_sweep(uow_factory: UnitOfWorkFactory, now: datetime.datetime) -> None:
    """Every 5 minutes, one transaction per user (BACKEND_DESIGN.md §15 Phase 4 jobs)."""
    storage = _storage()
    async with uow_factory(user_id=None) as uow:
        users = await list_active_user_ids(uow)
    expired = due = purged = 0
    for user_id in users:
        async with uow_factory(user_id=user_id) as uow:
            expired += await expire_pending_uploads(uow, storage, now=now)
            due += await publish_due_stages(uow, now=now)
            purged += await purge_raw_media(uow, storage, now=now)
            await clear_expired_provider_files(uow, now=now)
    if expired or due or purged:
        log.info("media_sweep", expired_uploads=expired, stages_due=due, raw_purged=purged)


def periodic_tasks() -> list[PeriodicTaskSpec]:
    return [
        PeriodicTaskSpec(
            name=MEDIA_SWEEP_TASK,
            periodic_id="media_sweep",
            cron="*/5 * * * *",
            queue="schedule",
            run=_media_sweep,
        )
    ]
