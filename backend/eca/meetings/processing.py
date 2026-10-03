"""Meeting processing state for AI-10 (slice 4.3): what ``work`` calls so ``meetings`` stays the
single writer of meetings, participants and recordings (BACKEND_DESIGN.md §5.2 Phase 4 table).

- ``begin_extraction``: conditional ``transcribed → extracting`` under the ``media:{recording}``
  lock; an unlinked upload gets its meeting (``origin = upload``) here.
- ``deterministic_mapping``: self-introductions and elimination (``meetings.speakers``).
- ``store_summary``, ``mark_processed``, ``extract_failed``, ``defer_extract``.
- ``prior_meetings``: the related earlier meetings (TECHNICAL_DESIGN.md §16.1).
"""

from __future__ import annotations

import datetime
import difflib
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select, text, update

from eca.meetings.events import (
    MEETING_CHANGED,
    MEETING_PROCESSED,
    MeetingChanged,
    MeetingProcessed,
)
from eca.meetings.models import meeting_participants_table, meetings_table, recordings_table
from eca.meetings.recordings import set_processing_status
from eca.meetings.speakers import (
    Attendee,
    Proposal,
    applied_labels,
    apply_proposals,
    elimination,
    introduction_proposals,
    publish_mapping_changed,
    unmatched_introductions,
)
from eca.meetings.transcripts import Segment
from eca.people import aliases_of, get_persons, get_self_person, match_names, normalize_alias
from eca.platform.events import NewEvent
from eca.platform.ids import uuid7
from eca.platform.outbox import publish
from eca.platform.uow import UnitOfWork

PRIOR_WINDOW = datetime.timedelta(days=60)
MIN_OVERLAP = 0.5
MIN_TITLE_SIMILARITY = 0.8
OUTSIDE_MATCH_SCORE = 0.9
_LOCK_SQL = text("SELECT pg_advisory_xact_lock(hashtextextended('media:' || :rid, 0))")


@dataclass(frozen=True)
class ExtractionStart:
    recording_id: UUID
    meeting_id: UUID
    source_item_id: UUID
    transcription_model: str
    transcript_hash: bytes
    user_retry: bool  # the stage starts from ``transcribed`` (first run or the user's retry)


@dataclass(frozen=True)
class MeetingRecord:
    """A meeting row as the meeting page and prep read it. ``summary`` is AI-derived (AI-10) and
    carries its provenance columns; calendar fields are SOURCE data."""

    id: UUID
    source_item_id: UUID
    origin: str
    title: str | None
    description: str | None
    starts_at: datetime.datetime
    ends_at: datetime.datetime
    timezone: str | None
    status: str
    processing_status: str
    conference_uri: str | None
    organizer_person_id: UUID | None
    series_key: str | None
    project_hint: str | None
    summary: dict[str, Any] | None
    summary_extraction_id: UUID | None
    summary_model: str | None
    summary_prompt_version: str | None
    summary_derived_at: datetime.datetime | None
    prep_brief: dict[str, Any] | None
    prep_brief_version: int
    version: int
    participant_ids: tuple[UUID, ...]


async def _lock(uow: UnitOfWork, recording_id: UUID) -> None:
    await uow.session.execute(_LOCK_SQL, {"rid": str(recording_id)})


async def _create_upload_meeting(uow: UnitOfWork, row: Any, now: datetime.datetime) -> UUID:
    """An unlinked upload becomes its own meeting (BACKEND_DESIGN.md §17.7 "Upload meetings")."""
    start = row.occurred_at or row.created_at or now
    end = start + datetime.timedelta(seconds=float(row.duration_s or 0) or 60)
    meeting_id = uuid7()
    await uow.session.execute(
        meetings_table.insert().values(
            id=meeting_id,
            user_id=uow.user_id,
            source_item_id=row.source_item_id,
            title=row.title,
            starts_at=start,
            ends_at=end,
            status="occurred",
            processing_status="processing",
            origin="upload",
            prep_brief_version=0,
            version=1,
        )
    )
    r = recordings_table
    await uow.session.execute(update(r).where(r.c.id == row.id).values(meeting_id=meeting_id))
    await publish(
        uow,
        NewEvent(
            MEETING_CHANGED,
            "meeting",
            meeting_id,
            MeetingChanged(meeting_id=meeting_id, status="occurred", version=1),
        ),
    )
    return meeting_id


async def begin_extraction(
    uow: UnitOfWork, recording_id: UUID, *, now: datetime.datetime
) -> ExtractionStart | None:
    """None when the recording is not waiting for (or in) meeting extraction."""
    await _lock(uow, recording_id)
    r = recordings_table
    row = (
        await uow.session.execute(
            select(r).where(r.c.user_id == uow.user_id, r.c.id == recording_id).with_for_update()
        )
    ).one_or_none()
    if (
        row is None
        or row.status not in ("transcribed", "extracting")
        or row.transcription_model is None
        or row.source_item_id is None
    ):
        return None
    meeting_id = row.meeting_id or await _create_upload_meeting(uow, row, now)
    if row.status == "transcribed":
        await uow.session.execute(
            update(r).where(r.c.id == row.id).values(status="extracting", version=r.c.version + 1)
        )
    await set_processing_status(uow, meeting_id, "processing")
    return ExtractionStart(
        recording_id=row.id,
        meeting_id=meeting_id,
        source_item_id=row.source_item_id,
        transcription_model=row.transcription_model,
        transcript_hash=bytes(row.transcript_hash or b""),
        user_retry=row.status == "transcribed",
    )


async def _attendees(uow: UnitOfWork, meeting_id: UUID) -> list[Attendee]:
    mp = meeting_participants_table
    ids = [
        r.person_id
        for r in await uow.session.execute(
            select(mp.c.person_id).where(mp.c.user_id == uow.user_id, mp.c.meeting_id == meeting_id)
        )
    ]
    persons = await get_persons(uow, ids)
    aliases = await aliases_of(uow, ids)
    out = []
    for pid in sorted(ids):
        ref = persons.get(pid)
        names = {normalize_alias(ref.display_name) if ref and ref.display_name else ""} | set(
            aliases.get(pid, [])
        )
        out.append(Attendee(pid, tuple(sorted(n for n in names if n)), bool(ref and ref.is_self)))
    return out


async def deterministic_mapping(
    uow: UnitOfWork, meeting_id: UUID, segments: list[Segment], *, now: datetime.datetime
) -> list[str]:
    """Self-introductions first, then elimination. Returns the labels newly applied."""
    attendees = await _attendees(uow, meeting_id)
    proposals: list[Proposal] = introduction_proposals(segments, attendees)
    for label, name in unmatched_introductions(segments, attendees).items():
        matches = await match_names(uow, name, limit=2)
        if len(matches) == 1 and matches[0].score >= OUTSIDE_MATCH_SCORE:
            proposals.append(Proposal(label, matches[0].person.id, 0.8, "self_introduction", "deterministic"))
    changed = await apply_proposals(uow, meeting_id, proposals, now=now)
    labels: list[str] = []
    for s in segments:
        if s.speaker_label and s.speaker_label not in labels:
            labels.append(s.speaker_label)
    self_p = await get_self_person(uow)
    last = elimination(labels, await applied_labels(uow, meeting_id), attendees, self_p.id)
    if last is not None:
        changed += await apply_proposals(uow, meeting_id, [last], now=now)
    if changed:
        m = meetings_table
        version = (await uow.session.execute(select(m.c.version).where(m.c.id == meeting_id))).scalar_one()
        await publish_mapping_changed(uow, meeting_id, sorted(set(changed)), int(version))
    return changed


async def record_ai_mappings(
    uow: UnitOfWork,
    meeting_id: UUID,
    proposals: list[Proposal],
    *,
    extraction_id: UUID,
    model: str | None,
    now: datetime.datetime,
) -> list[str]:
    """AI-10 ``speaker_mapping`` (resolved to persons by ``work``): auto-apply only at ≥ 0.9."""
    return await apply_proposals(
        uow, meeting_id, proposals, extraction_id=extraction_id, model=model, now=now
    )


async def store_summary(
    uow: UnitOfWork,
    meeting_id: UUID,
    *,
    summary: dict[str, Any],
    extraction_id: UUID,
    model: str | None,
    prompt_version: str,
    now: datetime.datetime,
) -> None:
    """The AI-10 summary, topics and grounded concerns with their provenance (an inference)."""
    m = meetings_table
    await uow.session.execute(
        update(m)
        .where(m.c.user_id == uow.user_id, m.c.id == meeting_id)
        .values(
            summary=summary,
            summary_extraction_id=extraction_id,
            summary_method="llm",
            summary_model=model,
            summary_prompt_version=prompt_version,
            summary_derived_at=now,
        )
    )


async def mark_processed(
    uow: UnitOfWork, recording_id: UUID, meeting_id: UUID, *, mapped_labels: list[str], now: datetime.datetime
) -> None:
    """Apply finished: the recording is ``ready``, the meeting ``processing_status = ready``."""
    r, m = recordings_table, meetings_table
    await uow.session.execute(
        update(r)
        .where(r.c.id == recording_id, r.c.status.in_(["transcribed", "extracting"]))
        .values(
            status="ready",
            processed_at=now,
            stage_attempts=0,
            error_code=None,
            next_attempt_at=None,
            version=r.c.version + 1,
        )
    )
    version = (
        await uow.session.execute(
            update(m)
            .where(m.c.user_id == uow.user_id, m.c.id == meeting_id)
            .values(processing_status="ready", version=m.c.version + 1)
            .returning(m.c.version, m.c.status)
        )
    ).one()
    if mapped_labels:
        await publish_mapping_changed(uow, meeting_id, sorted(set(mapped_labels)), version.version)
    await publish(
        uow,
        NewEvent(
            MEETING_PROCESSED,
            "meeting",
            meeting_id,
            MeetingProcessed(meeting_id=meeting_id, recording_id=recording_id),
        ),
    )
    await publish(
        uow,
        NewEvent(
            MEETING_CHANGED,
            "meeting",
            meeting_id,
            MeetingChanged(meeting_id=meeting_id, status=version.status, version=version.version),
        ),
    )


async def extract_failed(uow: UnitOfWork, recording_id: UUID, *, error_code: str) -> None:
    """``failed_permanent`` → the recording ``failed`` at ``extract``; the transcript stays
    searchable and the meeting shows "summary pending" (AI_PIPELINE.md §14)."""
    await _lock(uow, recording_id)
    r = recordings_table
    meeting_id = (
        await uow.session.execute(
            update(r)
            .where(
                r.c.user_id == uow.user_id,
                r.c.id == recording_id,
                r.c.status.in_(["transcribed", "extracting"]),
            )
            .values(
                status="failed",
                failed_stage="extract",
                error_code=error_code,
                next_attempt_at=None,
                version=r.c.version + 1,
            )
            .returning(r.c.meeting_id)
        )
    ).scalar_one_or_none()
    if meeting_id is not None:
        await set_processing_status(uow, meeting_id, "failed")


async def defer_extract(
    uow: UnitOfWork, recording_id: UUID, *, until: datetime.datetime, error_code: str
) -> None:
    """A budget refusal or a retryable error: ``media_sweep`` publishes ``RecordingStageDue`` then."""
    r = recordings_table
    await uow.session.execute(
        update(r)
        .where(r.c.user_id == uow.user_id, r.c.id == recording_id)
        .values(next_attempt_at=until, error_code=error_code)
    )


# ---------------------------------------------------------------- reads


async def meeting_record(uow: UnitOfWork, meeting_id: UUID) -> MeetingRecord | None:
    m, mp = meetings_table, meeting_participants_table
    row = (
        await uow.session.execute(
            select(m).where(m.c.user_id == uow.user_id, m.c.id == meeting_id, m.c.deleted_at.is_(None))
        )
    ).one_or_none()
    if row is None:
        return None
    people = tuple(
        r.person_id
        for r in await uow.session.execute(
            select(mp.c.person_id)
            .where(mp.c.user_id == uow.user_id, mp.c.meeting_id == meeting_id)
            .order_by(mp.c.person_id)
        )
    )
    return MeetingRecord(
        id=row.id,
        source_item_id=row.source_item_id,
        origin=row.origin,
        title=row.title,
        description=row.description,
        starts_at=row.starts_at,
        ends_at=row.ends_at,
        timezone=row.timezone,
        status=row.status,
        processing_status=row.processing_status,
        conference_uri=row.conference_uri,
        organizer_person_id=row.organizer_person_id,
        series_key=row.series_key,
        project_hint=row.project_hint,
        summary=row.summary,
        summary_extraction_id=row.summary_extraction_id,
        summary_model=row.summary_model,
        summary_prompt_version=row.summary_prompt_version,
        summary_derived_at=row.summary_derived_at,
        prep_brief=row.prep_brief,
        prep_brief_version=int(row.prep_brief_version),
        version=int(row.version),
        participant_ids=people,
    )


def normalized_title(title: str | None) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", (title or "").lower()).split())


def overlap(a: set[UUID], b: set[UUID]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


async def prior_meetings(uow: UnitOfWork, meeting_id: UUID, *, limit: int = 2) -> list[MeetingRecord]:
    """Earlier related meetings, most recent first (TECHNICAL_DESIGN.md §16.1): same series;
    else ≥ 50 % participant overlap (without the user) within 60 days; else title similarity
    ≥ 0.8 with at least one shared participant. Deterministic."""
    me = await meeting_record(uow, meeting_id)
    if me is None:
        return []
    self_p = await get_self_person(uow)
    mine = set(me.participant_ids) - {self_p.id}
    m, mp = meetings_table, meeting_participants_table
    rows = (
        await uow.session.execute(
            select(m.c.id, m.c.series_key, m.c.title, m.c.starts_at)
            .where(
                m.c.user_id == uow.user_id,
                m.c.id != meeting_id,
                m.c.starts_at < me.starts_at,
                m.c.status != "cancelled",
                m.c.deleted_at.is_(None),
            )
            .order_by(m.c.starts_at.desc(), m.c.id)
            .limit(500)
        )
    ).all()
    if not rows:
        return []
    people: dict[UUID, set[UUID]] = {r.id: set() for r in rows}
    for p in await uow.session.execute(
        select(mp.c.meeting_id, mp.c.person_id).where(
            mp.c.user_id == uow.user_id, mp.c.meeting_id.in_(list(people))
        )
    ):
        people[p.meeting_id].add(p.person_id)
    title = normalized_title(me.title)
    series = [r.id for r in rows if me.series_key and r.series_key == me.series_key]
    recent = [r for r in rows if r.starts_at >= me.starts_at - PRIOR_WINDOW]
    by_overlap = [r.id for r in recent if overlap(mine, people[r.id] - {self_p.id}) >= MIN_OVERLAP]
    by_title = [
        r.id
        for r in rows
        if title
        and difflib.SequenceMatcher(None, title, normalized_title(r.title)).ratio() >= MIN_TITLE_SIMILARITY
        and (people[r.id] - {self_p.id}) & mine
    ]
    chosen: list[UUID] = []
    for group in (series, by_overlap, by_title):
        for mid in group:
            if mid not in chosen:
                chosen.append(mid)
        if len(chosen) >= limit:
            break
    out = []
    for mid in chosen[:limit]:
        record = await meeting_record(uow, mid)
        if record is not None:
            out.append(record)
    return out
