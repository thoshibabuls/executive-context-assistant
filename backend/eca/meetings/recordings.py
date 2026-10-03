"""Uploaded recordings and transcript files (slice 4.1, BACKEND_DESIGN.md §11.4, §16.9).

Init → pre-signed URL → the browser uploads straight to storage → complete → ``RecordingUploaded``
→ the media pipeline (``meetings.media``). Idempotency (§10.1): ``Idempotency-Key`` on init (API),
``UNIQUE (user_id, sha256)`` so the same media returns the existing recording, and complete as a
conditional transition ``pending_upload → uploaded`` (a repeat is a no-op). Recordings are SOURCE
data (user uploads, §6.2): no AI-derived value is stored here.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from eca.ingestion import register_upload
from eca.intelligence import FileRef
from eca.meetings.events import (
    RECORDING_STAGE_DUE,
    RECORDING_UPLOADED,
    RecordingStageDue,
    RecordingUploaded,
)
from eca.meetings.models import meetings_table, recordings_table
from eca.platform.errors import Conflict, NotFound, ValidationFailed
from eca.platform.events import NewEvent
from eca.platform.ids import uuid7
from eca.platform.outbox import publish
from eca.platform.storage import ObjectStorage, PresignedUpload
from eca.platform.uow import UnitOfWork

GIB = 1024**3
MIB = 1024**2
MAX_MEDIA_BYTES = 2 * GIB
MAX_TRANSCRIPT_BYTES = 10 * MIB
PENDING_UPLOAD_TTL = datetime.timedelta(hours=24)
SUGGESTION_WINDOW = datetime.timedelta(hours=3)
RECENT_MEETINGS_WINDOW = datetime.timedelta(days=7)
MAX_SUGGESTIONS = 10
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MEDIA_TYPES = frozenset(
    {
        "audio/mpeg",
        "audio/mp4",
        "audio/x-m4a",
        "audio/wav",
        "audio/x-wav",
        "audio/webm",
        "audio/ogg",
        "audio/flac",
        "audio/aac",
        "video/mp4",
        "video/webm",
        "video/quicktime",
        "video/x-matroska",
    }
)
TRANSCRIPT_TYPES = frozenset({"text/vtt", "application/x-subrip", "text/plain", DOCX})
STAGE_START = {"prepare": "uploaded", "transcribe": "transcribing", "extract": "transcribed"}
LINKABLE = frozenset({"pending_upload", "uploaded", "preparing", "transcribing", "transcribed"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class RecordingView:
    id: UUID
    kind: str
    mime: str
    bytes: int
    title: str | None
    occurred_at: datetime.datetime | None
    meeting_id: UUID | None
    source_item_id: UUID | None
    status: str
    failed_stage: str | None
    error_code: str | None
    stage_attempts: int
    next_attempt_at: datetime.datetime | None
    duration_s: float | None
    transcription_model: str | None
    processed_at: datetime.datetime | None
    raw_purged_at: datetime.datetime | None
    version: int
    created_at: datetime.datetime | None


@dataclass(frozen=True)
class UploadInit:
    recording: RecordingView
    upload: PresignedUpload | None
    duplicate: bool


def normalize_mime(mime: str) -> str:
    return mime.split(";")[0].strip().lower()


def classify_upload(mime: str, size: int) -> str:
    """``media`` or ``transcript_file`` for an accepted type and size; else ``ValidationFailed``."""
    m = normalize_mime(mime)
    if m in MEDIA_TYPES:
        if not 0 < size <= MAX_MEDIA_BYTES:
            raise ValidationFailed("media files must be between 1 byte and 2 GiB", details={"reason": "size"})
        return "media"
    if m in TRANSCRIPT_TYPES:
        if not 0 < size <= MAX_TRANSCRIPT_BYTES:
            raise ValidationFailed("transcript files must be at most 10 MiB", details={"reason": "size"})
        return "transcript_file"
    raise ValidationFailed(
        "unsupported file type: audio, video or a transcript file", details={"reason": "type"}
    )


def parse_sha256(value: str) -> bytes:
    text = value.strip().lower()
    if not _SHA256.fullmatch(text):
        raise ValidationFailed("sha256 must be 64 hexadecimal characters")
    return bytes.fromhex(text)


def storage_key(user_id: UUID, recording_id: UUID, name: str | None = None) -> str:
    """Object keys hold IDs only, never a file name the user chose (§11.4)."""
    base = f"recordings/{user_id}/{recording_id}"
    return f"{base}/{name}" if name else base


def _view(r: Any) -> RecordingView:
    return RecordingView(
        id=r.id,
        kind=r.kind,
        mime=r.mime,
        bytes=int(r.bytes),
        title=r.title,
        occurred_at=r.occurred_at,
        meeting_id=r.meeting_id,
        source_item_id=r.source_item_id,
        status=r.status,
        failed_stage=r.failed_stage,
        error_code=r.error_code,
        stage_attempts=int(r.stage_attempts),
        next_attempt_at=r.next_attempt_at,
        duration_s=float(r.duration_s) if r.duration_s is not None else None,
        transcription_model=r.transcription_model,
        processed_at=r.processed_at,
        raw_purged_at=r.raw_purged_at,
        version=int(r.version),
        created_at=r.created_at,
    )


async def _row(uow: UnitOfWork, recording_id: UUID, *, for_update: bool = False) -> Any:
    t = recordings_table
    stmt = select(t).where(t.c.user_id == uow.user_id, t.c.id == recording_id)
    if for_update:
        stmt = stmt.with_for_update()
    row = (await uow.session.execute(stmt)).one_or_none()
    if row is None:
        raise NotFound("recording not found")
    return row


async def get_recording(uow: UnitOfWork, recording_id: UUID) -> RecordingView:
    return _view(await _row(uow, recording_id))


async def recording_for_meeting(uow: UnitOfWork, meeting_id: UUID) -> RecordingView | None:
    t = recordings_table
    row = (
        await uow.session.execute(select(t).where(t.c.user_id == uow.user_id, t.c.meeting_id == meeting_id))
    ).one_or_none()
    return None if row is None else _view(row)


async def recordings_by_source(uow: UnitOfWork, source_item_ids: list[UUID]) -> dict[UUID, RecordingView]:
    if not source_item_ids:
        return {}
    t = recordings_table
    rows = await uow.session.execute(
        select(t).where(t.c.user_id == uow.user_id, t.c.source_item_id.in_(source_item_ids))
    )
    return {r.source_item_id: _view(r) for r in rows}


async def list_recordings_page(
    uow: UnitOfWork, *, after: list[Any] | None, limit: int
) -> tuple[list[RecordingView], list[Any] | None]:
    """Newest first; keyset on ``(created_at, id)``."""
    t = recordings_table
    stmt = select(t).where(t.c.user_id == uow.user_id)
    if after:
        at, last_id = datetime.datetime.fromisoformat(str(after[0])), UUID(str(after[1]))
        stmt = stmt.where(or_(t.c.created_at < at, and_(t.c.created_at == at, t.c.id < last_id)))
    rows = (
        await uow.session.execute(stmt.order_by(t.c.created_at.desc(), t.c.id.desc()).limit(limit + 1))
    ).all()
    page, more = rows[:limit], len(rows) > limit
    key = [page[-1].created_at.isoformat(), str(page[-1].id)] if more and page else None
    return [_view(r) for r in page], key


async def _check_meeting(uow: UnitOfWork, meeting_id: UUID) -> None:
    m = meetings_table
    found = (
        await uow.session.execute(
            select(m.c.id, m.c.origin).where(
                m.c.user_id == uow.user_id, m.c.id == meeting_id, m.c.deleted_at.is_(None)
            )
        )
    ).one_or_none()
    if found is None:
        raise NotFound("meeting not found")
    if found.origin != "calendar":
        raise ValidationFailed("only calendar meetings can be linked")
    if await recording_for_meeting(uow, meeting_id) is not None:
        raise Conflict("this meeting already has a recording", details={"reason": "meeting_has_recording"})


async def init_upload(
    uow: UnitOfWork,
    storage: ObjectStorage,
    *,
    mime: str,
    size: int,
    sha256: str,
    title: str | None,
    occurred_at: datetime.datetime | None,
    meeting_id: UUID | None,
    now: datetime.datetime,
) -> UploadInit:
    """A known ``sha256`` returns the existing recording (no upload); else a ``pending_upload`` row
    and a pre-signed PUT URL limited to the declared type and size."""
    kind = classify_upload(mime, size)
    digest = parse_sha256(sha256)
    if occurred_at is not None and occurred_at.tzinfo is None:
        raise ValidationFailed("occurred_at must include a timezone")
    if title is not None and len(title) > 200:
        raise ValidationFailed("title must be at most 200 characters")
    t = recordings_table
    existing = (
        await uow.session.execute(select(t).where(t.c.user_id == uow.user_id, t.c.sha256 == digest))
    ).one_or_none()
    if existing is not None:
        return UploadInit(_view(existing), None, True)
    if meeting_id is not None:
        await _check_meeting(uow, meeting_id)
    assert uow.user_id is not None
    recording_id = uuid7()
    key = storage_key(uow.user_id, recording_id)
    content_type = normalize_mime(mime)
    inserted = (
        await uow.session.execute(
            insert(t)
            .values(
                id=recording_id,
                user_id=uow.user_id,
                kind=kind,
                mime=content_type,
                bytes=size,
                sha256=digest,
                title=(title or "").strip() or None,
                occurred_at=occurred_at,
                storage_key=key,
                meeting_id=meeting_id,
                status="pending_upload",
                stage_attempts=0,
                upload_expires_at=now + PENDING_UPLOAD_TTL,
                version=1,
            )
            .on_conflict_do_nothing()
            .returning(t.c.id)
        )
    ).scalar_one_or_none()
    if inserted is None:  # a concurrent init with the same media committed first
        row = (
            await uow.session.execute(select(t).where(t.c.user_id == uow.user_id, t.c.sha256 == digest))
        ).one()
        return UploadInit(_view(row), None, True)
    upload = storage.presign_put(key, content_type=content_type, max_bytes=size, now=now)
    return UploadInit(await get_recording(uow, recording_id), upload, False)


async def complete_upload(
    uow: UnitOfWork, storage: ObjectStorage, recording_id: UUID, *, now: datetime.datetime
) -> RecordingView:
    """``pending_upload → uploaded`` once the object exists with the declared size; registers the
    source item and publishes ``RecordingUploaded``. A repeat returns the current state."""
    row = await _row(uow, recording_id, for_update=True)
    if row.status != "pending_upload":
        return _view(row)
    info = await storage.head(row.storage_key)
    if info is None or info.size != row.bytes:
        raise Conflict("the upload is missing or incomplete", details={"reason": "upload_incomplete"})
    source_item_id = await register_upload(
        uow,
        recording_id=row.id,
        kind="recording" if row.kind == "media" else "transcript_file",
        sha256=bytes(row.sha256),
        occurred_at=row.occurred_at or now,
    )
    t = recordings_table
    await uow.session.execute(
        update(t)
        .where(t.c.id == row.id, t.c.status == "pending_upload")
        .values(status="uploaded", source_item_id=source_item_id, version=t.c.version + 1)
    )
    if row.meeting_id is not None:
        await set_processing_status(uow, row.meeting_id, "processing")
    await publish(
        uow,
        NewEvent(RECORDING_UPLOADED, "recording", row.id, RecordingUploaded(recording_id=row.id)),
    )
    return await get_recording(uow, recording_id)


async def set_processing_status(uow: UnitOfWork, meeting_id: UUID, status: str) -> None:
    m = meetings_table
    await uow.session.execute(
        update(m).where(m.c.user_id == uow.user_id, m.c.id == meeting_id).values(processing_status=status)
    )


async def retry_recording(uow: UnitOfWork, recording_id: UUID) -> RecordingView:
    """Only a ``failed`` recording: back to its failed stage's start, attempts reset, stage due."""
    row = await _row(uow, recording_id, for_update=True)
    if row.status != "failed" or row.failed_stage not in STAGE_START:
        raise Conflict("only a failed recording can be retried", details={"reason": "not_failed"})
    if row.raw_purged_at is not None and row.failed_stage != "extract":
        raise Conflict(
            "the media was deleted after 30 days; upload it again", details={"reason": "media_purged"}
        )
    t = recordings_table
    await uow.session.execute(
        update(t)
        .where(t.c.id == row.id, t.c.status == "failed")
        .values(
            status=STAGE_START[row.failed_stage],
            failed_stage=None,
            error_code=None,
            stage_attempts=0,
            next_attempt_at=None,
            version=t.c.version + 1,
        )
    )
    if row.meeting_id is not None:
        await set_processing_status(uow, row.meeting_id, "processing")
    await publish(
        uow,
        NewEvent(
            RECORDING_STAGE_DUE,
            "recording",
            row.id,
            RecordingStageDue(recording_id=row.id, stage=row.failed_stage),
        ),
    )
    return await get_recording(uow, recording_id)


async def link_meeting(uow: UnitOfWork, recording_id: UUID, meeting_id: UUID | None) -> RecordingView:
    """Link to a calendar meeting (or unlink) until meeting extraction has started (§16.9)."""
    row = await _row(uow, recording_id, for_update=True)
    if row.status not in LINKABLE and not (row.status == "failed" and row.failed_stage != "extract"):
        raise Conflict("meeting extraction has already started", details={"reason": "already_extracted"})
    if meeting_id == row.meeting_id:
        return _view(row)
    if meeting_id is not None:
        await _check_meeting(uow, meeting_id)
    t = recordings_table
    await uow.session.execute(
        update(t).where(t.c.id == row.id).values(meeting_id=meeting_id, version=t.c.version + 1)
    )
    if row.meeting_id is not None:
        await set_processing_status(uow, row.meeting_id, "none")
    if meeting_id is not None and row.status != "pending_upload":
        await set_processing_status(uow, meeting_id, "processing")
    return await get_recording(uow, recording_id)


@dataclass(frozen=True)
class MeetingSuggestion:
    id: UUID
    title: str | None
    starts_at: datetime.datetime
    ends_at: datetime.datetime
    reason: str  # overlaps | recent


async def meeting_suggestions(
    uow: UnitOfWork, *, occurred_at: datetime.datetime | None, now: datetime.datetime
) -> list[MeetingSuggestion]:
    """Calendar meetings overlapping ``occurred_at`` ± 3 h, else the 10 most recent of 7 days;
    meetings that already have a recording are left out (§16.9). Deterministic."""
    m, r = meetings_table, recordings_table
    taken = select(r.c.meeting_id).where(r.c.user_id == uow.user_id, r.c.meeting_id.is_not(None))
    base = select(m.c.id, m.c.title, m.c.starts_at, m.c.ends_at).where(
        m.c.user_id == uow.user_id,
        m.c.origin == "calendar",
        m.c.status != "cancelled",
        m.c.deleted_at.is_(None),
        m.c.id.not_in(taken),
    )
    if occurred_at is not None:
        rows = (
            await uow.session.execute(
                base.where(
                    m.c.starts_at < occurred_at + SUGGESTION_WINDOW,
                    m.c.ends_at > occurred_at - SUGGESTION_WINDOW,
                )
                .order_by(func.abs(func.extract("epoch", m.c.starts_at - occurred_at)), m.c.id)
                .limit(MAX_SUGGESTIONS)
            )
        ).all()
        if rows:
            return [MeetingSuggestion(x.id, x.title, x.starts_at, x.ends_at, "overlaps") for x in rows]
    rows = (
        await uow.session.execute(
            base.where(m.c.starts_at <= now, m.c.starts_at >= now - RECENT_MEETINGS_WINDOW)
            .order_by(m.c.starts_at.desc(), m.c.id)
            .limit(MAX_SUGGESTIONS)
        )
    ).all()
    return [MeetingSuggestion(x.id, x.title, x.starts_at, x.ends_at, "recent") for x in rows]


# ---------------------------------------------------------------- sweep, deletion


async def expire_pending_uploads(uow: UnitOfWork, storage: ObjectStorage, *, now: datetime.datetime) -> int:
    """Uploads not completed within 24 h are removed with their partial objects (§11.4)."""
    t = recordings_table
    rows = (
        await uow.session.execute(
            select(t.c.id, t.c.storage_key)
            .where(t.c.user_id == uow.user_id, t.c.status == "pending_upload", t.c.upload_expires_at < now)
            .with_for_update(skip_locked=True)
        )
    ).all()
    for row in rows:
        await storage.delete(row.storage_key)
    if rows:
        await uow.session.execute(delete(t).where(t.c.id.in_([r.id for r in rows])))
    return len(rows)


async def publish_due_stages(uow: UnitOfWork, *, now: datetime.datetime) -> int:
    """``RecordingStageDue`` for recordings whose retry or budget wait has passed (§15)."""
    t = recordings_table
    rows = (
        await uow.session.execute(
            select(t.c.id, t.c.status)
            .where(
                t.c.user_id == uow.user_id,
                t.c.next_attempt_at.is_not(None),
                t.c.next_attempt_at <= now,
                t.c.status.in_(["uploaded", "preparing", "transcribing", "transcribed", "extracting"]),
            )
            .with_for_update(skip_locked=True)
        )
    ).all()
    stage_of = {
        "uploaded": "prepare",
        "preparing": "prepare",
        "transcribing": "transcribe",
        "transcribed": "extract",
        "extracting": "extract",
    }
    for row in rows:
        stage = stage_of[row.status]
        await uow.session.execute(update(t).where(t.c.id == row.id).values(next_attempt_at=None))
        await publish(
            uow,
            NewEvent(
                RECORDING_STAGE_DUE, "recording", row.id, RecordingStageDue(recording_id=row.id, stage=stage)
            ),
        )
    return len(rows)


RAW_RETENTION_READY = datetime.timedelta(days=7)
RAW_RETENTION_REJECTED = datetime.timedelta(days=7)
RAW_RETENTION_FAILED = datetime.timedelta(days=30)


async def purge_raw_media(uow: UnitOfWork, storage: ObjectStorage, *, now: datetime.datetime) -> int:
    """Retention (TECHNICAL_DESIGN.md §9.3): the original and the prepared audio are deleted 7 days
    after processing or rejection and 30 days after a failure. Rows, transcripts and facts stay."""
    t = recordings_table
    due = or_(
        and_(t.c.status == "ready", t.c.processed_at < now - RAW_RETENTION_READY),
        and_(t.c.status == "rejected", t.c.updated_at < now - RAW_RETENTION_REJECTED),
        and_(t.c.status == "failed", t.c.updated_at < now - RAW_RETENTION_FAILED),
    )
    rows = (
        await uow.session.execute(
            select(t.c.id, t.c.storage_key, t.c.audio_storage_key)
            .where(t.c.user_id == uow.user_id, t.c.raw_purged_at.is_(None), due)
            .with_for_update(skip_locked=True)
        )
    ).all()
    for row in rows:
        await storage.delete(row.storage_key)
        if row.audio_storage_key:
            await storage.delete(row.audio_storage_key)
    if rows:
        await uow.session.execute(
            update(t)
            .where(t.c.id.in_([r.id for r in rows]))
            .values(raw_purged_at=now, provider_file_ref=None, provider_file_expires_at=None)
        )
    return len(rows)


async def clear_expired_provider_files(uow: UnitOfWork, *, now: datetime.datetime) -> int:
    """Provider files past their expiry are gone at the provider (48 h): drop the reference."""
    t = recordings_table
    result = await uow.session.execute(
        update(t)
        .where(
            t.c.user_id == uow.user_id,
            t.c.provider_file_expires_at.is_not(None),
            t.c.provider_file_expires_at < now,
        )
        .values(provider_file_ref=None, provider_file_expires_at=None)
    )
    return int(result.rowcount)  # type: ignore[attr-defined]


async def provider_file_refs(uow: UnitOfWork) -> list[FileRef]:
    """Provider files still referenced by the user's recordings (account deletion deletes them
    outside any transaction, before the objects)."""
    t = recordings_table
    rows = await uow.session.execute(
        select(t.c.provider_file_ref).where(t.c.user_id == uow.user_id, t.c.provider_file_ref.is_not(None))
    )
    return [
        FileRef(name=d["name"], uri=d["uri"], mime_type=d["mime_type"], sha256=d.get("sha256"))
        for d in (r.provider_file_ref for r in rows)
        if d
    ]


async def object_keys(uow: UnitOfWork) -> list[str]:
    """Every object key of the user's recordings (account deletion deletes them first, §13.3)."""
    t = recordings_table
    keys: list[str] = []
    for r in await uow.session.execute(
        select(t.c.storage_key, t.c.audio_storage_key).where(t.c.user_id == uow.user_id)
    ):
        keys.append(r.storage_key)
        if r.audio_storage_key:
            keys.append(r.audio_storage_key)
    return keys


async def delete_user_objects(uow: UnitOfWork, storage: ObjectStorage) -> int:
    keys = await object_keys(uow)
    for key in keys:
        await storage.delete(key)
    return len(keys)
