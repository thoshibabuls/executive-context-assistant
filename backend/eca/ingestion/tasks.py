"""Ingestion handlers and periodic tasks.

``SyncRequested`` (manual, periodic or webhook trigger) runs ``sync_mail`` in natural-key mode
(§7.4): the provider call happens outside any transaction, and the cursor lease plus the unique
source key make duplicate triggers harmless (RT-07).
"""

from __future__ import annotations

import datetime

from eca.connections import list_active_connections
from eca.connectors import ConnectorRegistry
from eca.identity import list_active_user_ids
from eca.ingestion.events import SYNC_REQUESTED, SyncRequested
from eca.ingestion.service import MAIL_RESOURCE, sync_mail
from eca.platform.clock import Clock
from eca.platform.events import HandlerContext, NewEvent, handles
from eca.platform.jobs import PeriodicTaskSpec
from eca.platform.outbox import publish
from eca.platform.uow import UnitOfWorkFactory

SYNC_HANDLER = "ingestion.sync"


@handles(SYNC_REQUESTED, name=SYNC_HANDLER, queue="sync", mode="natural_key")
async def on_sync_requested(ctx: HandlerContext) -> None:
    payload = ctx.payload
    assert isinstance(payload, SyncRequested)
    if payload.resource != MAIL_RESOURCE:
        return  # calendar sync is wired with the meetings module (slice 1.6)
    assert ctx.envelope.user_id is not None
    await sync_mail(
        ctx.factory,
        ctx.resources.get(ConnectorRegistry),
        user_id=ctx.envelope.user_id,
        connection_id=payload.connection_id,
        now=ctx.resources.get(Clock).now(),
        owner=f"job:{ctx.envelope.id}",
    )


async def request_syncs(
    uow_factory: UnitOfWorkFactory, now: datetime.datetime, *, trigger: str = "periodic"
) -> int:
    """Publish ``SyncRequested`` for every active connection of every active user."""
    async with uow_factory(user_id=None) as uow:
        users = await list_active_user_ids(uow)
    count = 0
    for user_id in users:
        async with uow_factory(user_id=user_id) as uow:
            for info in await list_active_connections(uow):
                await publish(
                    uow,
                    NewEvent(
                        event_type=SYNC_REQUESTED,
                        aggregate_type="connection",
                        aggregate_id=info.connection_id,
                        payload=SyncRequested(
                            connection_id=info.connection_id, resource=MAIL_RESOURCE, trigger=trigger
                        ),
                    ),
                )
                count += 1
    return count


async def _periodic_sync(uow_factory: UnitOfWorkFactory, now: datetime.datetime) -> None:
    await request_syncs(uow_factory, now)


def periodic_tasks() -> list[PeriodicTaskSpec]:
    return [
        PeriodicTaskSpec(
            name="eca.ingestion.request_syncs",
            periodic_id="request_syncs",
            cron="*/5 * * * *",
            queue="schedule",
            run=_periodic_sync,
        )
    ]
