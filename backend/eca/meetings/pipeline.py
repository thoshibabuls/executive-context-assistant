"""Media pipeline: ``media_prepare`` and ``transcribe`` (slice 4.2, BACKEND_DESIGN.md §15 Phase 4 jobs).

Both handlers run in natural-key mode on queue ``media``. Every transaction takes the advisory lock
``media:{recording}``; ffprobe, ffmpeg, object transfers and the AI-09 call run between
transactions, never inside one. A stage is claimed by a conditional status check, so a repeated or
late job is a no-op.

Failures (§15 "Media stages"): a retryable failure below the cap of 4 runs records
``stage_attempts + 1``, the error code and ``next_attempt_at`` (backoff 60 s · 2^(n-1), at most
1 h); ``media_sweep`` publishes ``RecordingStageDue`` when that time has passed. The 4th failure
marks the recording ``failed`` at the stage. Limit, checksum, no-audio and unreadable-file errors
reject the recording (terminal). A budget refusal is not an attempt.

Re-transcription is never automatic: ``transcribe`` does nothing for a recording that already has a
transcript version. Logs carry IDs and codes only, never transcript text.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import func, select, text, update

from eca.intelligence import AIClient, FileRef, run_transcribe
from eca.intelligence import default_budget_config as _budget_config
from eca.meetings.events import (
    RECORDING_PREPARED,
    RECORDING_STAGE_DUE,
    RECORDING_UPLOADED,
    TRANSCRIPT_STORED,
    RecordingPrepared,
    RecordingStageDue,
    RecordingUploaded,
    TranscriptStored,
)
from eca.meetings.media import (
    AUDIO_MIME,
    WINDOW_S,
    MediaError,
    MediaTools,
    cut_window,
    extract_audio,
    job_dir,
    probe,
    window_starts,
)
from eca.meetings.models import recordings_table
from eca.meetings.recordings import set_processing_status, storage_key
from eca.meetings.transcripts import (
    Segment,
    TranscriptParseError,
    duration_of,
    number,
    parse_transcript_file,
    replace_version,
    transcript_hash,
    validate_model_segments,
)
from eca.platform.clock import Clock
from eca.platform.config import get_settings
from eca.platform.events import HandlerContext, NewEvent, handles
from eca.platform.outbox import publish
from eca.platform.storage import ObjectStorage, StorageError, sha256_of
from eca.platform.uow import UnitOfWork, UnitOfWorkFactory

log = structlog.get_logger("eca.meetings.pipeline")

STAGE_CAP = 4
BACKOFF_BASE_S = 60
BACKOFF_MAX_S = 3600
PROVIDER_FILE_TTL = datetime.timedelta(hours=47)  # Google expires Files API uploads at 48 h
_LOCK_SQL = text("SELECT pg_advisory_xact_lock(hashtextextended('media:' || :rid, 0))")


@dataclass(frozen=True)
class _Claim:
    kind: str
    mime: str
    storage_key: str
    audio_storage_key: str | None
    sha256: bytes
    source_item_id: UUID
    meeting_id: UUID | None
    duration_s: float | None
    stage_attempts: int
    provider_file_ref: dict[str, Any] | None
    provider_file_expires_at: datetime.datetime | None
    other_media_s: float


def _now(ctx: HandlerContext) -> datetime.datetime:
    try:
        return ctx.resources.get(Clock).now()
    except LookupError:
        return datetime.datetime.now(datetime.UTC)


def _tools() -> MediaTools:
    s = get_settings()
    return MediaTools(ffmpeg=s.ffmpeg_path, ffprobe=s.ffprobe_path)


def backoff(attempts: int) -> datetime.timedelta:
    return datetime.timedelta(seconds=min(BACKOFF_BASE_S * 2 ** max(attempts - 1, 0), BACKOFF_MAX_S))


async def _lock(uow: UnitOfWork, recording_id: UUID) -> None:
    await uow.session.execute(_LOCK_SQL, {"rid": str(recording_id)})


async def _locked_row(uow: UnitOfWork, recording_id: UUID) -> Any:
    await _lock(uow, recording_id)
    t = recordings_table
    return (
        await uow.session.execute(
            select(t).where(t.c.user_id == uow.user_id, t.c.id == recording_id).with_for_update()
        )
    ).one_or_none()


async def _other_media_seconds(uow: UnitOfWork, recording_id: UUID, now: datetime.datetime) -> float:
    """Media hours of the user's other recordings of the last 7 x 24 h (not rejected)."""
    t = recordings_table
    total = (
        await uow.session.execute(
            select(func.coalesce(func.sum(t.c.duration_s), 0.0)).where(
                t.c.user_id == uow.user_id,
                t.c.id != recording_id,
                t.c.kind == "media",
                t.c.status != "rejected",
                t.c.created_at >= now - datetime.timedelta(days=7),
            )
        )
    ).scalar_one()
    return float(total)


def _claim(row: Any, other_media_s: float = 0.0) -> _Claim:
    return _Claim(
        kind=row.kind,
        mime=row.mime,
        storage_key=row.storage_key,
        audio_storage_key=row.audio_storage_key,
        sha256=bytes(row.sha256),
        source_item_id=row.source_item_id,
        meeting_id=row.meeting_id,
        duration_s=float(row.duration_s) if row.duration_s is not None else None,
        stage_attempts=int(row.stage_attempts),
        provider_file_ref=row.provider_file_ref,
        provider_file_expires_at=row.provider_file_expires_at,
        other_media_s=other_media_s,
    )


# ---------------------------------------------------------------- state transitions


async def _set(uow: UnitOfWork, recording_id: UUID, **values: Any) -> None:
    t = recordings_table
    await uow.session.execute(
        update(t).where(t.c.id == recording_id).values(**values, version=t.c.version + 1)
    )


async def _reject(
    uow: UnitOfWork, recording_id: UUID, code: str, *, meeting_id: UUID | None, now: datetime.datetime
) -> None:
    await _set(uow, recording_id, status="rejected", error_code=code, next_attempt_at=None, processed_at=now)
    if meeting_id is not None:
        await set_processing_status(uow, meeting_id, "failed")


async def _fail_attempt(
    uow: UnitOfWork,
    recording_id: UUID,
    *,
    stage: str,
    code: str,
    attempts: int,
    retryable: bool,
    meeting_id: UUID | None,
    now: datetime.datetime,
) -> str:
    """One failed run: below the cap a backoff; at the cap (or not retryable) ``failed``."""
    attempts += 1
    if retryable and attempts < STAGE_CAP:
        await _set(
            uow,
            recording_id,
            stage_attempts=attempts,
            error_code=code,
            next_attempt_at=now + backoff(attempts),
        )
        return "retry"
    await _set(
        uow,
        recording_id,
        status="failed",
        failed_stage=stage,
        stage_attempts=attempts,
        error_code=code,
        next_attempt_at=None,
    )
    if meeting_id is not None:
        await set_processing_status(uow, meeting_id, "failed")
    return "failed"


async def _defer(uow: UnitOfWork, recording_id: UUID, *, seconds: int, now: datetime.datetime) -> None:
    await _set(
        uow,
        recording_id,
        error_code="budget_deferred",
        next_attempt_at=now + datetime.timedelta(seconds=seconds),
    )


async def store_transcript(
    uow: UnitOfWork,
    recording_id: UUID,
    *,
    source_item_id: UUID,
    transcription_model: str,
    segments: list[Segment],
    duration_s: float | None,
) -> None:
    """One transaction: the version replaced atomically, the recording ``transcribed``, outbox
    ``TranscriptStored``."""
    await replace_version(
        uow, recording_id=recording_id, transcription_model=transcription_model, segments=segments
    )
    values: dict[str, Any] = {
        "status": "transcribed",
        "transcription_model": transcription_model,
        "transcript_hash": transcript_hash(segments),
        "stage_attempts": 0,
        "error_code": None,
        "next_attempt_at": None,
    }
    if duration_s is not None:
        values["duration_s"] = duration_s
    await _set(uow, recording_id, **values)
    await publish(
        uow,
        NewEvent(
            TRANSCRIPT_STORED,
            "recording",
            recording_id,
            TranscriptStored(
                recording_id=recording_id,
                source_item_id=source_item_id,
                transcription_model=transcription_model,
            ),
        ),
    )


# ---------------------------------------------------------------- media_prepare


@dataclass
class _Prepared:
    kind: str  # transcript | audio | reject | retry
    code: str | None = None
    model: str | None = None
    segments: list[Segment] = field(default_factory=list)
    key: str | None = None
    duration_s: float | None = None


async def prepare_recording(
    uow_factory: UnitOfWorkFactory,
    storage: ObjectStorage,
    *,
    user_id: UUID,
    recording_id: UUID,
    now: datetime.datetime,
) -> str:
    """``uploaded → preparing → transcribing`` (media) or ``→ transcribed`` (transcript files)."""
    async with uow_factory(user_id=user_id) as uow:
        row = await _locked_row(uow, recording_id)
        if row is None or row.status not in ("uploaded", "preparing") or row.source_item_id is None:
            return "skipped"
        if row.status == "uploaded":
            await _set(uow, recording_id, status="preparing")
        claim = _claim(row, await _other_media_seconds(uow, recording_id, now))

    limits = _budget_config().meetings
    result = _Prepared("retry", "media_unknown")
    try:
        with job_dir(get_settings().api_media_tmp_dir) as work:
            source = work / "input"
            await storage.download(claim.storage_key, source)
            if sha256_of(source) != claim.sha256:
                raise MediaError("checksum_mismatch", retryable=False)
            if claim.kind == "transcript_file":
                model, segments = parse_transcript_file(claim.mime, source.read_bytes())
                result = _Prepared("transcript", model=model, segments=segments)
            else:
                probed = await probe(_tools(), source)
                if not probed.has_audio:
                    raise MediaError("no_audio", retryable=False)
                if probed.duration_s > limits.max_upload_hours * 3600:
                    raise MediaError("duration_limit", retryable=False)
                if claim.other_media_s + probed.duration_s > limits.max_weekly_hours * 3600:
                    raise MediaError("weekly_limit", retryable=False)
                audio = work / "audio.ogg"
                await extract_audio(_tools(), source, audio, duration_s=probed.duration_s)
                key = storage_key(user_id, recording_id, "audio.ogg")
                await storage.put_file(key, audio, content_type=AUDIO_MIME)
                result = _Prepared("audio", key=key, duration_s=probed.duration_s)
    except TranscriptParseError:
        result = _Prepared("reject", "unreadable")
    except MediaError as exc:
        result = _Prepared("retry" if exc.retryable else "reject", exc.code)
    except StorageError:
        result = _Prepared("retry", "storage_unavailable")

    async with uow_factory(user_id=user_id) as uow:
        row = await _locked_row(uow, recording_id)
        if row is None or row.status != "preparing":
            return "skipped"
        code = result.code or "media_unknown"
        if result.kind == "reject":
            await _reject(uow, recording_id, code, meeting_id=claim.meeting_id, now=now)
            log.info("media_rejected", recording_id=str(recording_id), error_code=code)
            return "rejected"
        if result.kind == "retry":
            return await _fail_attempt(
                uow,
                recording_id,
                stage="prepare",
                code=code,
                attempts=int(row.stage_attempts),
                retryable=True,
                meeting_id=claim.meeting_id,
                now=now,
            )
        if result.kind == "transcript":
            assert result.model is not None
            await store_transcript(
                uow,
                recording_id,
                source_item_id=claim.source_item_id,
                transcription_model=result.model,
                segments=result.segments,
                duration_s=duration_of(result.segments),
            )
            return "transcribed"
        await _set(
            uow,
            recording_id,
            status="transcribing",
            audio_storage_key=result.key,
            duration_s=result.duration_s,
            stage_attempts=0,
            error_code=None,
            next_attempt_at=None,
        )
        await publish(
            uow,
            NewEvent(
                RECORDING_PREPARED, "recording", recording_id, RecordingPrepared(recording_id=recording_id)
            ),
        )
        return "prepared"


# ---------------------------------------------------------------- transcribe (AI-09)


def _file_ref(data: dict[str, Any] | None) -> FileRef | None:
    if not data:
        return None
    return FileRef(name=data["name"], uri=data["uri"], mime_type=data["mime_type"], sha256=data.get("sha256"))


def _file_json(ref: FileRef) -> dict[str, Any]:
    return {"name": ref.name, "uri": ref.uri, "mime_type": ref.mime_type, "sha256": ref.sha256}


@dataclass
class _Transcribed:
    ok: bool = False
    rows: list[tuple[int, int, str, str]] | None = None
    model: str | None = None
    error_code: str | None = None
    retryable: bool = False
    deferred_for_s: int | None = None


async def _windows(
    client: AIClient,
    audio: Path,
    work: Path,
    *,
    duration_s: float,
    user_id: UUID,
    attempt: int,
) -> _Transcribed:
    """The 60-minute window fallback: one call per window; labels become ``W<n> <label>``."""
    rows: list[tuple[int, int, str, str]] = []
    models: set[str] = set()
    for n, start in enumerate(window_starts(duration_s), start=1):
        length = int(min(WINDOW_S, duration_s - start + 1))
        piece = work / f"window{n}.ogg"
        await cut_window(_tools(), audio, piece, start_s=start, length_s=length)
        digest = sha256_of(piece).hex()
        ref = await client.upload_file(piece, mime_type=AUDIO_MIME, sha256=digest)
        try:
            out = await run_transcribe(
                client, ref, duration_s=length, user_id=user_id, attempt=attempt, use_fallback=attempt >= 3
            )
        finally:
            await client.delete_file(ref)
        if out.deferred_for_s is not None:
            return _Transcribed(error_code=out.error_code, deferred_for_s=out.deferred_for_s)
        if not out.ok:
            return _Transcribed(error_code=out.error_code or "transcribe_failed", retryable=True)
        rows += validate_model_segments(
            [(s.start_ms, s.end_ms, s.speaker, s.text) for s in out.segments],
            duration_ms=length * 1000,
            offset_ms=start * 1000,
            label_prefix=f"W{n} ",
        )
        if out.model:
            models.add(out.model)
    if not rows:
        return _Transcribed(error_code="empty_transcript", retryable=True)
    return _Transcribed(ok=True, rows=rows, model="+".join(sorted(models)) or None)


async def transcribe_recording(
    uow_factory: UnitOfWorkFactory,
    storage: ObjectStorage,
    client: AIClient,
    *,
    user_id: UUID,
    recording_id: UUID,
    now: datetime.datetime,
) -> str:
    async with uow_factory(user_id=user_id) as uow:
        row = await _locked_row(uow, recording_id)
        if row is None or row.status != "transcribing" or row.transcription_model is not None:
            return "skipped"  # never re-transcribe automatically
        if row.audio_storage_key is None or row.source_item_id is None:
            return "skipped"
        claim = _claim(row)
    attempt = claim.stage_attempts + 1
    duration_s = claim.duration_s or 0.0
    ref = _file_ref(claim.provider_file_ref)
    if claim.provider_file_expires_at is None or claim.provider_file_expires_at <= now:
        ref = None

    outcome = _Transcribed()
    new_ref: FileRef | None = None
    try:
        with job_dir(get_settings().api_media_tmp_dir) as work:
            audio = work / "audio.ogg"
            if ref is None:
                assert claim.audio_storage_key is not None
                await storage.download(claim.audio_storage_key, audio)
                new_ref = await client.upload_file(audio, mime_type=AUDIO_MIME, sha256=sha256_of(audio).hex())
                ref = new_ref
                async with uow_factory(user_id=user_id) as uow:
                    await _lock(uow, recording_id)
                    await _set(
                        uow,
                        recording_id,
                        provider_file_ref=_file_json(new_ref),
                        provider_file_expires_at=now + PROVIDER_FILE_TTL,
                    )
            single = await run_transcribe(
                client,
                ref,
                duration_s=duration_s,
                user_id=user_id,
                attempt=attempt,
                use_fallback=attempt >= 3,
            )
            if single.deferred_for_s is not None:
                outcome = _Transcribed(error_code=single.error_code, deferred_for_s=single.deferred_for_s)
            elif single.ok:
                rows = validate_model_segments(
                    [(s.start_ms, s.end_ms, s.speaker, s.text) for s in single.segments],
                    duration_ms=int(duration_s * 1000),
                )
                outcome = (
                    _Transcribed(ok=True, rows=rows, model=single.model)
                    if rows
                    else _Transcribed(error_code="empty_transcript", retryable=True)
                )
            elif single.truncated:
                if not audio.is_file():
                    assert claim.audio_storage_key is not None
                    await storage.download(claim.audio_storage_key, audio)
                outcome = await _windows(
                    client, audio, work, duration_s=duration_s, user_id=user_id, attempt=attempt
                )
            else:
                outcome = _Transcribed(error_code=single.error_code, retryable=single.retryable)
    except MediaError as exc:
        outcome = _Transcribed(error_code=exc.code, retryable=exc.retryable)
    except StorageError:
        outcome = _Transcribed(error_code="storage_unavailable", retryable=True)

    file_deleted = False
    if outcome.ok and ref is not None:
        try:
            await client.delete_file(ref)
            file_deleted = True
        except Exception:
            log.warning("provider_file_delete_failed", recording_id=str(recording_id))

    async with uow_factory(user_id=user_id) as uow:
        row = await _locked_row(uow, recording_id)
        if row is None or row.status != "transcribing" or row.transcription_model is not None:
            return "skipped"
        if outcome.deferred_for_s is not None:
            await _defer(uow, recording_id, seconds=outcome.deferred_for_s, now=now)
            return "deferred"
        if not outcome.ok or outcome.rows is None:
            result = await _fail_attempt(
                uow,
                recording_id,
                stage="transcribe",
                code=outcome.error_code or "transcribe_failed",
                attempts=int(row.stage_attempts),
                retryable=outcome.retryable,
                meeting_id=claim.meeting_id,
                now=now,
            )
            log.info("transcribe_failed", recording_id=str(recording_id), error_code=outcome.error_code)
            return result
        if file_deleted:
            await _set(uow, recording_id, provider_file_ref=None, provider_file_expires_at=None)
        segments = number([(s, e, label, body) for s, e, label, body in outcome.rows])
        await store_transcript(
            uow,
            recording_id,
            source_item_id=claim.source_item_id,
            transcription_model=outcome.model or "unknown",
            segments=segments,
            duration_s=None,
        )
        log.info("transcript_stored", recording_id=str(recording_id), segments=len(segments))
        return "transcribed"


# ---------------------------------------------------------------- handlers


@handles(RECORDING_UPLOADED, name="meetings.media_prepare", queue="media", mode="natural_key")
async def on_recording_uploaded(ctx: HandlerContext) -> None:
    payload = ctx.payload
    assert isinstance(payload, RecordingUploaded) and ctx.envelope.user_id is not None
    await prepare_recording(
        ctx.factory,
        ctx.resources.get(ObjectStorage),
        user_id=ctx.envelope.user_id,
        recording_id=payload.recording_id,
        now=_now(ctx),
    )


@handles(RECORDING_PREPARED, name="meetings.transcribe", queue="media", mode="natural_key")
async def on_recording_prepared(ctx: HandlerContext) -> None:
    payload = ctx.payload
    assert isinstance(payload, RecordingPrepared) and ctx.envelope.user_id is not None
    await transcribe_recording(
        ctx.factory,
        ctx.resources.get(ObjectStorage),
        ctx.resources.get(AIClient),
        user_id=ctx.envelope.user_id,
        recording_id=payload.recording_id,
        now=_now(ctx),
    )


@handles(RECORDING_STAGE_DUE, name="meetings.media_stage_due", queue="media", mode="natural_key")
async def on_media_stage_due(ctx: HandlerContext) -> None:
    payload = ctx.payload
    assert isinstance(payload, RecordingStageDue) and ctx.envelope.user_id is not None
    if payload.stage == "prepare":
        await prepare_recording(
            ctx.factory,
            ctx.resources.get(ObjectStorage),
            user_id=ctx.envelope.user_id,
            recording_id=payload.recording_id,
            now=_now(ctx),
        )
    elif payload.stage == "transcribe":
        await transcribe_recording(
            ctx.factory,
            ctx.resources.get(ObjectStorage),
            ctx.resources.get(AIClient),
            user_id=ctx.envelope.user_id,
            recording_id=payload.recording_id,
            now=_now(ctx),
        )
