"""Privacy handlers and the nightly retention task (BACKEND_DESIGN.md §13.3)."""

from __future__ import annotations

import datetime

import httpx

from eca.connections import TokenCrypto
from eca.identity import USER_DELETION_REQUESTED, UserDeletionRequested
from eca.platform.clock import Clock
from eca.platform.events import HandlerContext, Resources, handles
from eca.platform.jobs import PeriodicTaskSpec
from eca.platform.storage import ObjectStorage
from eca.platform.uow import UnitOfWorkFactory
from eca.privacy.events import SOURCE_PURGE_REQUESTED, SourcePurgeRequested
from eca.privacy.service import run_account_deletion, run_retention, run_source_purge

RETENTION_TASK = "eca.privacy.retention"


def _optional(resources: Resources, kind: type) -> object | None:
    try:
        return resources.get(kind)
    except LookupError:
        return None


@handles(
    USER_DELETION_REQUESTED,
    name="privacy.delete_account",
    queue="events",
    mode="natural_key",
    runs_while_deleting=True,
)
async def on_user_deletion_requested(ctx: HandlerContext) -> None:
    payload = ctx.payload
    assert isinstance(payload, UserDeletionRequested)
    crypto = _optional(ctx.resources, TokenCrypto)
    http = _optional(ctx.resources, httpx.AsyncClient)
    storage = _optional(ctx.resources, ObjectStorage)
    await run_account_deletion(
        ctx.factory,
        user_id=payload.user_id,
        job_id=payload.deletion_job_id,
        crypto=crypto if isinstance(crypto, TokenCrypto) else None,
        http=http if isinstance(http, httpx.AsyncClient) else None,
        now=ctx.resources.get(Clock).now(),
        storage=storage if isinstance(storage, ObjectStorage) else None,
    )


@handles(SOURCE_PURGE_REQUESTED, name="privacy.purge_source", queue="events", mode="natural_key")
async def on_source_purge_requested(ctx: HandlerContext) -> None:
    payload = ctx.payload
    assert isinstance(payload, SourcePurgeRequested)
    assert ctx.envelope.user_id is not None
    await run_source_purge(
        ctx.factory,
        user_id=ctx.envelope.user_id,
        job_id=payload.deletion_job_id,
        now=ctx.resources.get(Clock).now(),
    )


async def _retention(uow_factory: UnitOfWorkFactory, now: datetime.datetime) -> None:
    await run_retention(uow_factory, now=now)


def periodic_tasks() -> list[PeriodicTaskSpec]:
    return [
        PeriodicTaskSpec(
            name=RETENTION_TASK, periodic_id="retention", cron="17 3 * * *", queue="schedule", run=_retention
        )
    ]
