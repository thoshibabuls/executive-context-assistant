"""Account deletion, source purge, retention and "Your data" (slice 1.9, BACKEND_DESIGN.md §13).

Deletion jobs are ordered (children before parents, no cascades, §13.2), idempotent and
resumable: every step can run again after a crash; ``deletion_jobs.progress`` records the steps
done for operators. Each step is its own short worker transaction for the user.

Account deletion order (§13.3): uploaded objects (Phase 4) → revoke tokens → chat → retrieval
(chunks, traces) → work (mentions first: they point at evidence) → communication → meetings →
intelligence → people → ingestion → connections → final step (idempotency keys, feedback,
sessions, the user's event consumptions and outbox rows, audit rows replaced by one content-free
record, the job row, the user row) in one transaction.

Source purge (§13.3), per batch of the connection's source items: chunks and continuation links
→ mentions → work (evidence quotes redacted, AI-only items deleted, user-touched items kept with
``has_source_gap``) → messages and conversations → meetings → extractions → source items
(tombstones where redacted evidence still points at them). Then evidence orphaned by later
batches, cursors and the connection.
"""

from __future__ import annotations

import datetime
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx
import structlog
from sqlalchemy import delete, select, text, update
from sqlalchemy.dialects.postgresql import insert

from eca import (
    attention,
    chat,
    communication,
    connections,
    identity,
    ingestion,
    intelligence,
    meetings,
    people,
    projects,
    retrieval,
    work,
)
from eca.platform.audit import audit_log_table, record_audit
from eca.platform.errors import NotFound
from eca.platform.events import NewEvent
from eca.platform.feedback import purge_user_feedback
from eca.platform.idempotency import purge_expired_keys, purge_user_keys
from eca.platform.ids import uuid7
from eca.platform.outbox import publish, purge_dispatched, purge_user_events
from eca.platform.storage import ObjectStorage
from eca.platform.uow import UnitOfWork, UnitOfWorkFactory
from eca.privacy.events import SOURCE_PURGE_REQUESTED, SourcePurgeRequested
from eca.privacy.models import deletion_jobs_table

log = structlog.get_logger("eca.privacy")

SOURCE_BATCH = 200
AUDIT_RETENTION_DAYS = 365
OUTBOX_RETENTION_DAYS = 7

Step = Callable[[UnitOfWork], Awaitable[None]]


# ---------------------------------------------------------------- job bookkeeping


async def _job(uow: UnitOfWork, job_id: UUID) -> dict[str, Any] | None:
    j = deletion_jobs_table
    row = (
        await uow.session.execute(select(j.c.status, j.c.progress).where(j.c.id == job_id).with_for_update())
    ).one_or_none()
    if row is None:
        return None
    return {"status": row.status, "progress": dict(row.progress or {})}


async def _set_progress(
    uow: UnitOfWork,
    job_id: UUID,
    progress: dict[str, Any],
    *,
    now: datetime.datetime,
    status: str = "running",
) -> None:
    j = deletion_jobs_table
    await uow.session.execute(
        update(j).where(j.c.id == job_id).values(progress=progress, status=status, updated_at=now)
    )


async def _step_done(uow: UnitOfWork, job_id: UUID, step: str, *, now: datetime.datetime) -> None:
    job = await _job(uow, job_id)
    if job is None:
        return
    done = list(job["progress"].get("done", []))
    if step not in done:
        done.append(step)
    await _set_progress(uow, job_id, {**job["progress"], "done": done}, now=now)


# ---------------------------------------------------------------- account deletion


async def _work_step(uow: UnitOfWork) -> None:
    await people.purge_mentions(uow)
    await work.purge_user(uow)
    await purge_user_feedback(uow)


_ACCOUNT_STEPS: tuple[tuple[str, Step], ...] = (
    ("chat", chat.purge_user),
    ("attention", attention.purge_user),  # notifications, reminders (reference persons), push subscriptions
    ("retrieval", retrieval.purge_user),
    ("work", _work_step),
    ("communication", communication.purge_user),
    ("meetings", meetings.purge_user),
    ("intelligence", intelligence.purge_user),
    ("projects", projects.purge_user),  # after work (items reference projects), before people (members)
    ("people", people.purge_user),
    ("ingestion", ingestion.purge_user),
    ("connections", connections.purge_user),
)


async def _final_step(uow: UnitOfWork, job_id: UUID, steps: int) -> None:
    assert uow.user_id is not None
    user_id = uow.user_id
    await purge_user_keys(uow)
    await identity.purge_sessions(uow)
    await purge_user_events(uow, user_id)
    a = audit_log_table
    await uow.session.execute(delete(a).where(a.c.user_id == user_id))
    await record_audit(uow, "account_deleted", actor="system", anonymous=True, metadata={"steps": steps})
    await uow.session.execute(delete(deletion_jobs_table).where(deletion_jobs_table.c.user_id == user_id))
    await identity.delete_user_row(uow)


async def run_account_deletion(
    factory: UnitOfWorkFactory,
    *,
    user_id: UUID,
    job_id: UUID,
    crypto: connections.TokenCrypto | None,
    http: httpx.AsyncClient | None,
    now: datetime.datetime,
    storage: ObjectStorage | None = None,
    delete_provider_file: Callable[[intelligence.FileRef], Awaitable[None]] | None = None,
) -> bool:
    """Run (or resume) the account deletion job. False when there is nothing left to do."""
    async with factory(user_id=user_id) as uow:
        job = await _job(uow, job_id)
        if job is None or job["status"] == "done":
            return False
        done = set(job["progress"].get("done", []))
        await _set_progress(uow, job_id, job["progress"], now=now)
    if "objects" not in done:
        # Uploaded media live in object storage, outside the database: they go first (§13.3).
        # Provider (Files API) uploads are deleted outside any transaction; they expire in 48 h.
        if delete_provider_file is not None:
            async with factory(user_id=user_id) as uow:
                refs = await meetings.provider_file_refs(uow)
            for ref in refs:
                await delete_provider_file(ref)
        async with factory(user_id=user_id) as uow:
            if storage is not None:
                await meetings.delete_user_objects(uow, storage)
            await _step_done(uow, job_id, "objects", now=now)
    if "tokens" not in done:
        # Revocation is a network call per connection: its own transaction, before any data goes.
        async with factory(user_id=user_id) as uow:
            await connections.revoke_all_tokens(uow, crypto, http, now=now)
            await _step_done(uow, job_id, "tokens", now=now)
    for name, step in _ACCOUNT_STEPS:
        if name in done:
            continue
        async with factory(user_id=user_id) as uow:
            await step(uow)
            await _step_done(uow, job_id, name, now=now)
        log.info("account_deletion_step", step=name)
    async with factory(user_id=user_id) as uow:
        await _final_step(uow, job_id, len(_ACCOUNT_STEPS) + 3)
    log.info("account_deleted")
    return True


# ---------------------------------------------------------------- source purge


async def request_source_purge(uow: UnitOfWork, connection_id: UUID) -> UUID:
    """Queue the purge of a (disconnected) connection's data; one active purge job per user."""
    j = deletion_jobs_table
    job_id = uuid7()
    inserted = (
        await uow.session.execute(
            insert(j)
            .values(
                id=job_id,
                user_id=uow.user_id,
                kind="source_purge",
                status="pending",
                progress={"connections": [str(connection_id)], "done": []},
            )
            .on_conflict_do_nothing()
            .returning(j.c.id)
        )
    ).scalar_one_or_none()
    if inserted is None:
        row = (
            await uow.session.execute(
                select(j.c.id, j.c.progress)
                .where(j.c.kind == "source_purge", j.c.status.in_(["pending", "running"]))
                .with_for_update()
            )
        ).one()
        job_id = row.id
        progress = dict(row.progress or {})
        pending = list(progress.get("connections", []))
        if str(connection_id) not in pending:
            pending.append(str(connection_id))
        await uow.session.execute(
            update(j).where(j.c.id == job_id).values(progress={**progress, "connections": pending})
        )
    await publish(
        uow,
        NewEvent(
            SOURCE_PURGE_REQUESTED,
            "deletion_job",
            job_id,
            SourcePurgeRequested(deletion_job_id=job_id, connection_id=connection_id),
        ),
    )
    return UUID(str(job_id))


async def _purge_batch(uow: UnitOfWork, source_ids: list[UUID], *, now: datetime.datetime) -> list[UUID]:
    """One batch of sources, in one transaction. Returns the sources kept as tombstones."""
    extraction_ids = await intelligence.extraction_ids_for_sources(uow, source_ids)
    conversation_ids = await communication.conversation_ids_for_sources(uow, source_ids)
    meeting_ids = await meetings.meeting_ids_for_sources(uow, source_ids)
    await retrieval.purge_sources(uow, source_ids)  # chunks reference sources, conversations, meetings
    await retrieval.detach_meetings(uow, meeting_ids)  # transcript chunks of uploads stay (Phase 4)
    await work.detach_meetings(uow, meeting_ids)  # decisions from uploads stay (Phase 4)
    await work.delete_links(uow, "conversation", conversation_ids)
    await people.purge_mentions_for_sources(uow, source_ids)
    await work.purge_sources(uow, source_ids, extraction_ids)
    await work.detach_conversations(uow, conversation_ids)
    await communication.purge_sources(uow, source_ids)
    await meetings.purge_sources(uow, source_ids)
    await intelligence.delete_extractions(uow, extraction_ids)
    keep = await work.sources_still_referenced(uow, source_ids)
    await ingestion.purge_sources(uow, source_ids, keep=keep, now=now)
    return sorted(keep)


async def _purge_connection(
    factory: UnitOfWorkFactory, user_id: UUID, job_id: UUID, connection_id: UUID, *, now: datetime.datetime
) -> None:
    kept: list[UUID] = []
    while True:
        async with factory(user_id=user_id) as uow:
            batch = await ingestion.source_ids_for_connection(uow, connection_id, limit=SOURCE_BATCH)
            if not batch:
                break
            kept.extend(await _purge_batch(uow, batch, now=now))
    async with factory(user_id=user_id) as uow:
        if kept:
            # Items deleted in a later batch leave earlier batches' redacted evidence unreferenced.
            await work.delete_unreferenced_evidence(uow, kept)
            still = await work.sources_still_referenced(uow, kept)
            await ingestion.purge_sources(uow, [k for k in kept if k not in still], keep=(), now=now)
        await communication.detach_connection(uow, connection_id)
        await connections.purge_connection(uow, connection_id)
        await _step_done(uow, job_id, str(connection_id), now=now)
        await record_audit(
            uow, "source_purged", actor="system", target_type="connection", target_id=connection_id
        )


async def run_source_purge(
    factory: UnitOfWorkFactory, *, user_id: UUID, job_id: UUID, now: datetime.datetime
) -> int:
    """Purge every queued connection of the job; returns the number purged in this run."""
    purged = 0
    while True:
        async with factory(user_id=user_id) as uow:
            job = await _job(uow, job_id)
            if job is None or job["status"] == "done":
                return purged
            progress = job["progress"]
            todo = [c for c in progress.get("connections", []) if c not in progress.get("done", [])]
            if not todo:
                await _set_progress(uow, job_id, progress, now=now, status="done")
                return purged
            await _set_progress(uow, job_id, progress, now=now)
        connection_id = UUID(todo[0])
        async with factory(user_id=user_id) as uow:
            status = {c.id: c.status for c in await connections.list_connections(uow)}.get(connection_id)
        if status is None or status == "active":
            # Already purged, or reconnected after the request (purge applies to revoked ones only).
            async with factory(user_id=user_id) as uow:
                await _step_done(uow, job_id, str(connection_id), now=now)
            continue
        await _purge_connection(factory, user_id, job_id, connection_id, now=now)
        purged += 1


# ---------------------------------------------------------------- retention


@dataclass(frozen=True)
class RetentionReport:
    users: int
    bodies_purged: int
    ai_calls_deleted: int
    outbox_deleted: int


async def run_retention(factory: UnitOfWorkFactory, *, now: datetime.datetime) -> RetentionReport:
    """Nightly retention (TECHNICAL_DESIGN.md §9.3): bodies, AI call logs, audit, outbox, keys."""
    async with factory(user_id=None) as uow:
        ai_deleted = await intelligence.purge_old_calls(uow, now=now)
        a = audit_log_table
        await uow.session.execute(
            delete(a).where(a.c.created_at < now - datetime.timedelta(days=AUDIT_RETENTION_DAYS))
        )
        outbox_deleted = await purge_dispatched(
            uow, cutoff=now - datetime.timedelta(days=OUTBOX_RETENTION_DAYS)
        )
        user_ids = await identity.list_active_user_ids(uow)
    bodies = 0
    for user_id in user_ids:
        async with factory(user_id=user_id) as uow:
            bodies += await communication.purge_bodies(uow, now=now)
            await retrieval.purge_unretained(uow)  # purged bodies are not retrievable (§9.7)
            await retrieval.purge_old_traces(uow, now=now)  # retrieval traces: 90 days (§6.2)
            await attention.purge_expired(uow, now=now)  # notifications 30 days, closed reminders 90 (§17.6)
            await purge_expired_keys(uow, now=now)
    log.info("retention_done", users=len(user_ids), bodies=bodies, ai_calls=ai_deleted, outbox=outbox_deleted)
    return RetentionReport(len(user_ids), bodies, ai_deleted, outbox_deleted)


# ---------------------------------------------------------------- "Your data"

# Read-only counts across modules for the user's own data (RLS scopes every table to the user).
_DATA_COUNTS = {
    "messages": "SELECT count(*) FROM messages WHERE deleted_at IS NULL",
    "messages_with_body": "SELECT count(*) FROM messages WHERE body_purged_at IS NULL AND deleted_at IS NULL",
    "conversations": "SELECT count(*) FROM conversations WHERE deleted_at IS NULL",
    "meetings": "SELECT count(*) FROM meetings WHERE deleted_at IS NULL",
    "people": "SELECT count(*) FROM persons WHERE merged_into_id IS NULL AND deleted_at IS NULL",
    "work_items": "SELECT count(*) FROM work_items WHERE merged_into_id IS NULL",
    "decisions": "SELECT count(*) FROM decisions",
    "evidence_quotes": "SELECT count(*) FROM evidence",
    "ai_extractions": "SELECT count(*) FROM extractions",
    "source_items": "SELECT count(*) FROM source_items WHERE deleted_at IS NULL",
    "search_index_chunks": "SELECT count(*) FROM chunks",
    "projects": "SELECT count(*) FROM projects WHERE deleted_at IS NULL",
    "chat_messages": "SELECT count(*) FROM chat_messages",
}


@dataclass(frozen=True)
class DataSummary:
    counts: dict[str, int]
    connections: list[dict[str, Any]]
    deletion_jobs: list[dict[str, Any]]
    retention: dict[str, str]


async def data_summary(uow: UnitOfWork) -> DataSummary:
    counts = {}
    for name, sql in _DATA_COUNTS.items():
        counts[name] = int((await uow.session.execute(text(sql))).scalar_one())
    conns = [
        {"id": str(c.id), "provider": c.provider, "account_email": c.account_email, "status": c.status}
        for c in await connections.list_connections(uow)
    ]
    j = deletion_jobs_table
    jobs = [
        {"id": str(r.id), "kind": r.kind, "status": r.status, "requested_at": r.requested_at.isoformat()}
        for r in await uow.session.execute(
            select(j.c.id, j.c.kind, j.c.status, j.c.requested_at).order_by(j.c.requested_at.desc()).limit(10)
        )
    ]
    retention = {
        "message_bodies_prefiltered": f"{communication.PREFILTERED_BODY_DAYS} days",
        "message_bodies_relevant": f"{communication.RELEVANT_BODY_DAYS} days",
        "ai_call_logs": f"{intelligence.AI_CALLS_RETENTION_DAYS} days",
        "audit_log": f"{AUDIT_RETENTION_DAYS} days, content-free",
        "facts_and_evidence": "until you delete them, disconnect with purge, or delete your account",
    }
    return DataSummary(counts, conns, jobs, retention)


async def get_deletion_job(uow: UnitOfWork, job_id: UUID) -> dict[str, Any]:
    j = deletion_jobs_table
    row = (
        await uow.session.execute(
            select(j.c.id, j.c.kind, j.c.status, j.c.progress, j.c.updated_at).where(j.c.id == job_id)
        )
    ).one_or_none()
    if row is None:
        raise NotFound("deletion job not found")
    return {
        "id": str(row.id),
        "kind": row.kind,
        "status": row.status,
        "progress": row.progress,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
