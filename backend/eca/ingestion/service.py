"""Sync orchestration, source items and stage transitions (BACKEND_DESIGN.md §7.5, §10, §11.1).

``sync_mail`` runs one sync of one connection's mailbox:
1. take the cursor lease (one run per connection and resource, RT-07);
2. for each page, in one transaction: upsert the page's ``source_items`` (unique on
   connection, kind and external ID), publish ``SourceItemStored`` for each new item, and save the
   page token;
3. after the last page, store the new cursor and release the lease.
A crash keeps the old cursor and the last committed page token, so the next run resumes without
gaps, and the unique key turns re-fetched items into no-ops (RT-06).
"""

from __future__ import annotations

import datetime
import hashlib
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import Select, select, text, update
from sqlalchemy.dialects.postgresql import insert

from eca.connections import (
    CursorState,
    acquire_lease,
    advance_cursor,
    cursor_obtained_at,
    get_connection,
    release_after_failure,
    reset_cursor,
    save_page_token,
)
from eca.connectors import ConnectionInfo, ConnectorRegistry, NormalizedMessage, SyncBatch
from eca.identity import list_active_user_ids
from eca.ingestion.events import (
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
from eca.ingestion.models import source_items_table
from eca.ingestion.stages import MAX_STAGE_ATTEMPTS, SLA, check_transition
from eca.platform.errors import CursorExpired, NotFound
from eca.platform.events import NewEvent
from eca.platform.ids import uuid7
from eca.platform.outbox import publish
from eca.platform.uow import UnitOfWork, UnitOfWorkFactory

log = structlog.get_logger("eca.ingestion")

MAIL_RESOURCE = "mail"
CALENDAR_RESOURCE = "calendar"


@dataclass(frozen=True)
class SyncReport:
    ran: bool
    pages: int = 0
    fetched: int = 0
    stored: int = 0
    cursor: str | None = None


@dataclass(frozen=True)
class SourceItem:
    id: UUID
    user_id: UUID
    connection_id: UUID | None
    kind: str
    provider: str
    external_id: str
    external_thread_id: str | None
    content_hash: bytes
    categories: tuple[str, ...]
    occurred_at: datetime.datetime
    content: dict[str, object] | None
    stage: str


BIN_CATEGORIES = frozenset({"trash", "spam"})


def in_bin(categories: Sequence[str]) -> bool:
    """Trash or Spam (BACKEND_DESIGN.md §9.2)."""
    return bool(BIN_CATEGORIES & set(categories))


async def _publish_bin_change(uow: UnitOfWork, source_item_id: UUID, kind: str, trashed: bool) -> None:
    if trashed:
        payload: SourceItemTrashed | SourceItemRestored = SourceItemTrashed(
            source_item_id=source_item_id, kind=kind
        )
    else:
        payload = SourceItemRestored(source_item_id=source_item_id, kind=kind)
    await publish(
        uow,
        NewEvent(
            SOURCE_ITEM_TRASHED if trashed else SOURCE_ITEM_RESTORED, "source_item", source_item_id, payload
        ),
    )


async def mark_binned(
    uow: UnitOfWork, info: ConnectionInfo, *, kind: str, external_ids: Sequence[str], trashed: bool
) -> int:
    """Moved to (``trashed``) or out of Trash/Spam at the provider: flag, categories, event (§9.2)."""
    if not external_ids:
        return 0
    t = source_items_table
    rows = (
        await uow.session.execute(
            select(t.c.id, t.c.categories).where(
                t.c.connection_id == info.connection_id,
                t.c.kind == kind,
                t.c.external_id.in_(list(external_ids)),
                t.c.deleted_at.is_(None),
                t.c.trashed.is_not(trashed),
            )
        )
    ).all()
    for row in rows:
        categories = [c for c in (row.categories or []) if c not in BIN_CATEGORIES]
        if trashed:
            categories.append("trash")
        await uow.session.execute(
            update(t).where(t.c.id == row.id).values(trashed=trashed, categories=categories or ["inbox"])
        )
        await _publish_bin_change(uow, row.id, kind, trashed)
    return len(rows)


async def trashed_among(uow: UnitOfWork, source_item_ids: Sequence[UUID]) -> set[UUID]:
    """The given source items that are in Trash or Spam (excluded from reply state, §9.2)."""
    if not source_item_ids:
        return set()
    t = source_items_table
    rows = await uow.session.execute(
        select(t.c.id).where(t.c.user_id == uow.user_id, t.c.id.in_(list(source_item_ids)), t.c.trashed)
    )
    return {r.id for r in rows}


async def store_messages(uow: UnitOfWork, info: ConnectionInfo, messages: Sequence[NormalizedMessage]) -> int:
    """Upsert one page of messages; publish ``SourceItemStored`` for each new row."""
    stored = 0
    t = source_items_table
    for m in messages:
        item_id = uuid7()
        inserted = (
            await uow.session.execute(
                insert(t)
                .values(
                    id=item_id,
                    user_id=uow.user_id,
                    connection_id=info.connection_id,
                    kind="message",
                    provider=info.provider,
                    external_id=m.external_id,
                    external_thread_id=m.thread_external_id,
                    content_hash=m.content_hash,
                    provider_version=m.provider_version,
                    categories=list(m.categories),
                    occurred_at=m.sent_at,
                    trashed=in_bin(m.categories),
                    content=m.content(),
                    raw_metadata={"deep_link": m.deep_link} if m.deep_link else {},
                )
                .on_conflict_do_nothing(index_elements=["connection_id", "kind", "external_id"])
                .returning(t.c.id)
            )
        ).scalar_one_or_none()
        if inserted is None:
            # Already stored: metadata (categories) only; content is immutable per content_hash (§6.2).
            before = (
                await uow.session.execute(
                    select(t.c.id, t.c.trashed).where(
                        t.c.connection_id == info.connection_id,
                        t.c.kind == "message",
                        t.c.external_id == m.external_id,
                        t.c.categories != list(m.categories),
                    )
                )
            ).one_or_none()
            if before is not None:
                await uow.session.execute(
                    update(t)
                    .where(t.c.id == before.id)
                    .values(categories=list(m.categories), trashed=in_bin(m.categories))
                )
                if bool(before.trashed) != in_bin(m.categories):
                    await _publish_bin_change(uow, before.id, "message", in_bin(m.categories))
            continue
        stored += 1
        await publish(
            uow,
            NewEvent(
                event_type=SOURCE_ITEM_STORED,
                aggregate_type="source_item",
                aggregate_id=item_id,
                payload=SourceItemStored(source_item_id=item_id, kind="message"),
            ),
        )
    return stored


PageFetcher = Callable[[str | None, str | None], Awaitable[SyncBatch[Any]]]
PageStorer = Callable[[UnitOfWork, ConnectionInfo, SyncBatch[Any]], Awaitable[int]]


async def _run_sync(
    uow_factory: UnitOfWorkFactory,
    *,
    user_id: UUID,
    connection_id: UUID,
    resource: str,
    owner: str,
    now: datetime.datetime,
    fetch: Callable[[ConnectionInfo], PageFetcher],
    store: PageStorer,
    max_cursor_age: datetime.timedelta | None = None,
) -> SyncReport:
    """Lease → pages (each committed with its items and page token) → cursor (§11.1)."""
    async with uow_factory(user_id=user_id) as uow:
        info = await get_connection(uow, connection_id)
        state = await acquire_lease(uow, connection_id=connection_id, resource=resource, owner=owner, now=now)
        if state is not None and max_cursor_age is not None and state.cursor is not None:
            obtained = await cursor_obtained_at(uow, connection_id=connection_id, resource=resource)
            if obtained is not None and now - obtained > max_cursor_age:
                await reset_cursor(uow, connection_id=connection_id, resource=resource, owner=owner)
                state = CursorState(cursor=None, page_token=None, import_state="none")
    if state is None:
        log.info("sync_skipped_lease_held", connection_id=str(connection_id), resource=resource)
        return SyncReport(ran=False)
    page = fetch(info)
    cursor, token = state.cursor, state.page_token
    pages = fetched = stored = 0
    try:
        while True:
            try:
                batch = await page(cursor, token)
            except CursorExpired:
                log.warning("sync_cursor_expired", connection_id=str(connection_id), resource=resource)
                async with uow_factory(user_id=user_id) as uow:
                    await reset_cursor(uow, connection_id=connection_id, resource=resource, owner=owner)
                cursor = token = None
                continue
            async with uow_factory(user_id=user_id) as uow:
                stored += await store(uow, info, batch)
                await save_page_token(
                    uow,
                    connection_id=connection_id,
                    resource=resource,
                    owner=owner,
                    page_token=batch.next_page_token,
                    processed=len(batch.items),
                )
            pages += 1
            fetched += len(batch.items)
            if batch.next_page_token is None:
                break
            token = batch.next_page_token
        async with uow_factory(user_id=user_id) as uow:
            await advance_cursor(
                uow,
                connection_id=connection_id,
                resource=resource,
                owner=owner,
                cursor=batch.high_water_cursor,
                now=now,
            )
    except BaseException:
        async with uow_factory(user_id=user_id) as uow:
            await release_after_failure(uow, connection_id=connection_id, resource=resource, owner=owner)
        raise
    log.info(
        "sync_completed",
        connection_id=str(connection_id),
        resource=resource,
        pages=pages,
        fetched=fetched,
        stored=stored,
    )
    return SyncReport(ran=True, pages=pages, fetched=fetched, stored=stored, cursor=batch.high_water_cursor)


async def _store_mail_page(uow: UnitOfWork, info: ConnectionInfo, batch: SyncBatch[Any]) -> int:
    stored = await store_messages(uow, info, batch.items)
    if batch.deleted_external_ids:
        await mark_deleted(uow, info, kind="message", external_ids=batch.deleted_external_ids)
    await mark_binned(uow, info, kind="message", external_ids=batch.trashed_external_ids, trashed=True)
    await mark_binned(uow, info, kind="message", external_ids=batch.restored_external_ids, trashed=False)
    return stored


async def sync_mail(
    uow_factory: UnitOfWorkFactory,
    connectors: ConnectorRegistry,
    *,
    user_id: UUID,
    connection_id: UUID,
    now: datetime.datetime,
    owner: str,
) -> SyncReport:
    def fetch(info: ConnectionInfo) -> PageFetcher:
        connector = connectors.mail(info)

        async def page(cursor: str | None, token: str | None) -> SyncBatch[Any]:
            return await connector.list_messages(cursor=cursor, page_token=token, now=now)

        return page

    return await _run_sync(
        uow_factory,
        user_id=user_id,
        connection_id=connection_id,
        resource=MAIL_RESOURCE,
        owner=owner,
        now=now,
        fetch=fetch,
        store=_store_mail_page,
    )


async def sync_calendar(
    uow_factory: UnitOfWorkFactory,
    connectors: ConnectorRegistry,
    *,
    user_id: UUID,
    connection_id: UUID,
    now: datetime.datetime,
    owner: str,
) -> SyncReport:
    """Calendar sync (§11.3); a cursor older than a day is replaced by a full window sync."""

    def fetch(info: ConnectionInfo) -> PageFetcher:
        connector = connectors.calendar(info)

        async def page(cursor: str | None, token: str | None) -> SyncBatch[Any]:
            return await connector.list_events(cursor=cursor, page_token=token, now=now)

        return page

    return await _run_sync(
        uow_factory,
        user_id=user_id,
        connection_id=connection_id,
        resource=CALENDAR_RESOURCE,
        owner=owner,
        now=now,
        fetch=fetch,
        store=store_events,
        max_cursor_age=datetime.timedelta(days=1),
    )


async def store_events(uow: UnitOfWork, info: ConnectionInfo, batch: SyncBatch[Any]) -> int:
    """Upsert calendar events; unchanged ``etag`` → skip; changed → content updated, re-staged."""
    t = source_items_table
    stored = 0
    for ev in batch.items:
        content = event_content(ev)
        digest = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).digest()
        existing = (
            await uow.session.execute(
                select(t.c.id, t.c.provider_version).where(
                    t.c.connection_id == info.connection_id,
                    t.c.kind == "calendar_event",
                    t.c.external_id == ev.external_id,
                )
            )
        ).one_or_none()
        if existing is not None and existing.provider_version == ev.provider_version:
            continue
        item_id = existing.id if existing is not None else uuid7()
        if existing is None:
            await uow.session.execute(
                insert(t).values(
                    id=item_id,
                    user_id=uow.user_id,
                    connection_id=info.connection_id,
                    kind="calendar_event",
                    provider=info.provider,
                    external_id=ev.external_id,
                    external_thread_id=ev.series_external_id,
                    content_hash=digest,
                    provider_version=ev.provider_version,
                    categories=[],
                    occurred_at=ev.start,
                    trashed=False,
                    content=content,
                    raw_metadata={},
                )
            )
        else:
            await uow.session.execute(
                update(t)
                .where(t.c.id == item_id)
                .values(
                    content=content,
                    content_hash=digest,
                    provider_version=ev.provider_version,
                    occurred_at=ev.start,
                    stage="fetched",
                    stage_attempts=0,
                    stage_updated_at=text("now()"),
                    next_attempt_at=None,
                )
            )
        stored += 1
        await publish(
            uow,
            NewEvent(
                SOURCE_ITEM_STORED,
                "source_item",
                item_id,
                SourceItemStored(source_item_id=item_id, kind="calendar_event"),
            ),
        )
    return stored


UPLOAD_PROVIDER = "upload"


async def register_upload(
    uow: UnitOfWork,
    *,
    recording_id: UUID,
    kind: str,
    sha256: bytes,
    occurred_at: datetime.datetime,
) -> UUID:
    """The ``source_items`` row of an uploaded recording or transcript file (BACKEND_DESIGN.md
    §11.4): provider ``upload``, no connection, external ID = the recording ID, ``content_hash`` =
    the media SHA-256. Stage ``normalized``: the meeting pipeline tracks its own stages on
    ``recordings``, so the source-item reconciler never picks it up. Idempotent per recording."""
    if kind not in ("recording", "transcript_file"):
        raise ValueError(f"unknown upload kind {kind!r}")
    t = source_items_table
    existing = (
        await uow.session.execute(
            select(t.c.id).where(
                t.c.provider == UPLOAD_PROVIDER, t.c.kind == kind, t.c.external_id == str(recording_id)
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return UUID(str(existing))
    item_id = uuid7()
    await uow.session.execute(
        insert(t).values(
            id=item_id,
            user_id=uow.user_id,
            connection_id=None,
            kind=kind,
            provider=UPLOAD_PROVIDER,
            external_id=str(recording_id),
            content_hash=sha256,
            categories=[],
            occurred_at=occurred_at,
            trashed=False,
            content=None,
            raw_metadata={},
            stage="normalized",
            stage_attempts=0,
            stage_updated_at=text("now()"),
        )
    )
    return item_id


async def set_upload_occurred_at(
    uow: UnitOfWork, source_item_id: UUID, occurred_at: datetime.datetime
) -> None:
    """The upload's meeting time once it is known (a calendar link or the upload meeting)."""
    t = source_items_table
    await uow.session.execute(
        update(t)
        .where(t.c.id == source_item_id, t.c.provider == UPLOAD_PROVIDER)
        .values(occurred_at=occurred_at)
    )


def event_content(ev: Any) -> dict[str, Any]:
    return {
        "external_id": ev.external_id,
        "series_external_id": ev.series_external_id,
        "ical_uid": ev.ical_uid,
        "start": ev.start.isoformat(),
        "end": ev.end.isoformat(),
        "timezone": ev.timezone,
        "title": ev.title,
        "description": ev.description,
        "attendees": [
            {"email": a.email, "display_name": a.display_name, "response": a.response} for a in ev.attendees
        ],
        "organizer": {"email": ev.organizer.email, "display_name": ev.organizer.display_name}
        if ev.organizer
        else None,
        "conference_uri": ev.conference_uri,
        "status": ev.status,
    }


async def mark_deleted(
    uow: UnitOfWork, info: ConnectionInfo, *, kind: str, external_ids: Sequence[str]
) -> int:
    """Provider permanently deleted items (§9.3): tombstone, then ``SourceItemDeleted``."""
    t = source_items_table
    rows = (
        await uow.session.execute(
            update(t)
            .where(
                t.c.connection_id == info.connection_id,
                t.c.kind == kind,
                t.c.external_id.in_(list(external_ids)),
                t.c.deleted_at.is_(None),
            )
            .values(deleted_at=text("now()"))
            .returning(t.c.id)
        )
    ).all()
    for row in rows:
        await publish(
            uow,
            NewEvent(
                SOURCE_ITEM_DELETED,
                "source_item",
                row.id,
                SourceItemDeleted(source_item_id=row.id, kind=kind),
            ),
        )
    return len(rows)


async def get_source_item(uow: UnitOfWork, source_item_id: UUID, *, for_update: bool = False) -> SourceItem:
    t = source_items_table
    stmt = select(
        t.c.id,
        t.c.user_id,
        t.c.connection_id,
        t.c.kind,
        t.c.provider,
        t.c.external_id,
        t.c.external_thread_id,
        t.c.content_hash,
        t.c.categories,
        t.c.occurred_at,
        t.c.content,
        t.c.stage,
    ).where(t.c.id == source_item_id)
    if for_update:
        stmt = stmt.with_for_update()
    row = (await uow.session.execute(stmt)).one_or_none()
    if row is None:
        raise NotFound(f"source item {source_item_id} not found")
    return SourceItem(
        id=row.id,
        user_id=row.user_id,
        connection_id=row.connection_id,
        kind=row.kind,
        provider=row.provider,
        external_id=row.external_id,
        external_thread_id=row.external_thread_id,
        content_hash=bytes(row.content_hash),
        categories=tuple(row.categories),
        occurred_at=row.occurred_at,
        content=row.content,
        stage=row.stage,
    )


async def set_stage(
    uow: UnitOfWork,
    source_item_id: UUID,
    *,
    expected: Sequence[str],
    new: str,
    error_code: str | None = None,
) -> bool:
    """Conditional stage transition; False when the item is not in an ``expected`` stage."""
    for current in expected:
        check_transition(current, new)
    t = source_items_table
    result = await uow.session.execute(
        update(t)
        .where(t.c.id == source_item_id, t.c.stage.in_(list(expected)))
        .values(
            stage=new,
            stage_attempts=0,
            stage_updated_at=text("now()"),
            next_attempt_at=None,
            last_error_code=error_code,
        )
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def set_stage_error(uow: UnitOfWork, source_item_id: UUID, *, error_code: str) -> None:
    """Record a stage error without moving the stage (the reconciler or a retry continues)."""
    t = source_items_table
    await uow.session.execute(update(t).where(t.c.id == source_item_id).values(last_error_code=error_code))


async def defer_stage(
    uow: UnitOfWork, source_item_id: UUID, *, until: datetime.datetime, error_code: str
) -> None:
    """Keep the stage and hold the reconciler off until ``until`` (budget deferral,
    AI_COST_MODEL.md §7.2): the stage-SLA scan re-publishes ``SourceItemStageDue`` after it."""
    t = source_items_table
    await uow.session.execute(
        update(t).where(t.c.id == source_item_id).values(next_attempt_at=until, last_error_code=error_code)
    )


async def source_times(uow: UnitOfWork, source_item_ids: Sequence[UUID]) -> dict[UUID, datetime.datetime]:
    """``occurred_at`` of the user's source items (R2 re-applies extractions in source order)."""
    if not source_item_ids:
        return {}
    t = source_items_table
    rows = await uow.session.execute(
        select(t.c.id, t.c.occurred_at).where(t.c.user_id == uow.user_id, t.c.id.in_(list(source_item_ids)))
    )
    return {r.id: r.occurred_at for r in rows if r.occurred_at is not None}


async def items_in_stage(uow: UnitOfWork, stage: str) -> list[UUID]:
    t = source_items_table
    rows = await uow.session.execute(
        select(t.c.id).where(t.c.stage == stage).order_by(t.c.occurred_at, t.c.id)
    )
    return [r.id for r in rows]


_STUCK_SQL = text(
    """
    SELECT id, stage, stage_attempts FROM source_items
     WHERE stage = ANY(:stages) AND deleted_at IS NULL
       AND (CASE stage WHEN 'fetched' THEN stage_updated_at < :now - :sla_fetched
                       WHEN 'extract_pending' THEN stage_updated_at < :now - :sla_extract
                       ELSE stage_updated_at < :now - :sla_extracted END)
       AND (next_attempt_at IS NULL OR next_attempt_at <= :now)
     ORDER BY stage_updated_at, id
     LIMIT :limit
       FOR UPDATE SKIP LOCKED
    """
)


async def reconcile_user_stages(uow: UnitOfWork, *, now: datetime.datetime, limit: int = 500) -> int:
    """One user's stage-SLA scan (§7.5): re-publish ``SourceItemStageDue`` for stuck items."""
    rows = (
        await uow.session.execute(
            _STUCK_SQL,
            {
                "stages": list(SLA),
                "now": now,
                "sla_fetched": SLA["fetched"],
                "sla_extract": SLA["extract_pending"],
                "sla_extracted": SLA["extracted"],
                "limit": limit,
            },
        )
    ).all()
    t = source_items_table
    for row in rows:
        if row.stage_attempts + 1 >= MAX_STAGE_ATTEMPTS:
            await uow.session.execute(
                update(t)
                .where(t.c.id == row.id)
                .values(stage="needs_attention", last_error_code="stage_sla_exhausted")
            )
            continue
        await uow.session.execute(
            update(t)
            .where(t.c.id == row.id)
            .values(next_attempt_at=now + SLA[row.stage], stage_attempts=t.c.stage_attempts + 1)
        )
        await publish(
            uow,
            NewEvent(
                event_type=SOURCE_ITEM_STAGE_DUE,
                aggregate_type="source_item",
                aggregate_id=row.id,
                payload=SourceItemStageDue(source_item_id=row.id, stage=row.stage),
            ),
        )
    return len(rows)


async def reconcile_stages(uow_factory: UnitOfWorkFactory, *, now: datetime.datetime) -> int:
    """All users, one transaction per user (worker role; users read through §7.6's policy)."""
    async with uow_factory(user_id=None) as uow:
        users = await list_active_user_ids(uow)
    total = 0
    for user_id in users:
        async with uow_factory(user_id=user_id) as uow:
            total += await reconcile_user_stages(uow, now=now)
    if total:
        log.info("source_item_stages_reconciled", republished=total)
    return total


def visible_source_ids(
    user_id: UUID,
    *,
    exclude_calendar_connections: Sequence[UUID] = (),
    only: Sequence[UUID] | None = None,
) -> Select[Any]:
    """Source items a retriever may use (CONTEXT_ARCHITECTURE.md §9.7, §9.10): the user's, not
    deleted, not trashed, and no calendar event of a connection that lost the calendar scope.
    Returned as a subquery so the retriever applies it inside its own statement, before ranking."""
    t = source_items_table
    stmt = select(t.c.id).where(t.c.user_id == user_id, t.c.deleted_at.is_(None), ~t.c.trashed)
    excluded = list(exclude_calendar_connections)
    if excluded:
        stmt = stmt.where(~((t.c.kind == "calendar_event") & t.c.connection_id.in_(excluded)))
    if only is not None:
        stmt = stmt.where(t.c.id.in_(list(only)))
    return stmt


async def visible_among(
    uow: UnitOfWork, source_item_ids: Sequence[UUID], *, exclude_calendar_connections: Sequence[UUID] = ()
) -> set[UUID]:
    """The visible subset of ``source_item_ids`` (for evidence quotes packed with items)."""
    ids = list(source_item_ids)
    if not ids or uow.user_id is None:
        return set()
    stmt = visible_source_ids(
        uow.user_id, exclude_calendar_connections=exclude_calendar_connections, only=ids
    )
    return {UUID(str(v)) for v in (await uow.session.execute(stmt)).scalars()}
