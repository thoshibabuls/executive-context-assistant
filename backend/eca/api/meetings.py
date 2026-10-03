"""Recording and meeting routes (Phase 4, BACKEND_DESIGN.md §11.4, §16.9).

Slice 4.1: upload init (``Idempotency-Key`` required, 10 per hour, sha256 dedupe), the local
storage adapter's signed PUT target, complete, status, list, retry, meeting link and the upload
dialog's meeting suggestions. Slice 4.3: the meeting page and missed-meeting view
(``GET /meetings/{id}``, deterministic reads) and speaker confirmation (``PUT
/meetings/{id}/speakers``: ``meetings.set_speaker_mapping`` and ``work.remap_speaker_items`` in one
transaction, authority 5). Slice 4.4: the prep view (``GET /meetings/{id}/prep``, deterministic)
and the once-per-version AI-11 request (``POST /meetings/{id}/prep/asks``). Statuses and limits
are decided in the domain modules; this module only maps them to HTTP.
"""

from __future__ import annotations

import datetime
import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from eca import attention, meetings, people, work
from eca.api.chat import _limiter as limiter
from eca.api.common import (
    Cursors,
    Factory,
    IdempotencyKey,
    IfMatch,
    User,
    as_json,
    etag,
    idempotent,
    limit_of,
    now,
    page_body,
    parse_if_match,
    request_key,
)
from eca.api.problems import problem_response
from eca.api.work import decision_json, evidence_json, item_json
from eca.platform.errors import Conflict, NotFound, UpstreamUnavailable, ValidationFailed
from eca.platform.feedback import record_feedback
from eca.platform.storage import LocalObjectStorage, ObjectStorage
from eca.platform.uow import UnitOfWork

router = APIRouter(prefix="/api/v1")


def storage_of(request: Request) -> ObjectStorage:
    storage = getattr(request.app.state, "storage", None)
    if not isinstance(storage, ObjectStorage):
        raise UpstreamUnavailable("object storage is not configured")
    return storage


def recording_json(r: meetings.RecordingView) -> dict[str, Any]:
    """Status by stage (§16.9). Uploads are SOURCE data: no AI-derived field here."""
    return as_json(
        {
            "id": r.id,
            "kind": r.kind,
            "mime": r.mime,
            "bytes": r.bytes,
            "title": r.title,
            "occurred_at": r.occurred_at,
            "meeting_id": r.meeting_id,
            "status": r.status,
            "failed_stage": r.failed_stage,
            "error_code": r.error_code,
            "stage_attempts": r.stage_attempts,
            "next_attempt_at": r.next_attempt_at,
            "duration_s": r.duration_s,
            "transcription_model": r.transcription_model,
            "processed_at": r.processed_at,
            "raw_purged_at": r.raw_purged_at,
            "version": r.version,
            "created_at": r.created_at,
        }
    )


class InitUpload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mime: str = Field(min_length=3, max_length=120)
    bytes: int = Field(gt=0)
    sha256: str = Field(min_length=64, max_length=64)
    title: str | None = Field(default=None, max_length=200)
    occurred_at: datetime.datetime | None = None
    meeting_id: UUID | None = None


@router.post("/recordings", status_code=201)
async def init_recording(
    body: InitUpload, request: Request, user: User, factory: Factory, key: IdempotencyKey
) -> JSONResponse:
    """Upload init: a pre-signed URL (201), or the existing recording for known media (200)."""
    if not key:
        raise ValidationFailed("Idempotency-Key is required for uploads")
    limiter(request).check(user.user_id, "upload")
    storage = storage_of(request)

    async def run(uow: UnitOfWork) -> tuple[int, dict[str, Any]]:
        got = await meetings.init_upload(
            uow,
            storage,
            mime=body.mime,
            size=body.bytes,
            sha256=body.sha256,
            title=body.title,
            occurred_at=body.occurred_at,
            meeting_id=body.meeting_id,
            now=now(),
        )
        upload = (
            None
            if got.upload is None
            else {
                "url": got.upload.url,
                "method": got.upload.method,
                "headers": got.upload.headers,
                "expires_at": got.upload.expires_at.isoformat(),
            }
        )
        return (200 if got.duplicate else 201), {
            "recording": recording_json(got.recording),
            "upload": upload,
            "duplicate": got.duplicate,
        }

    response = await idempotent(factory, user, request, key, body.model_dump(mode="json"), run)
    if response.status_code == 201 and (location := _location(response)) is not None:
        response.headers["Location"] = location
    return response


def _location(response: JSONResponse) -> str | None:
    data = json.loads(bytes(response.body))
    rec = data.get("recording") if isinstance(data, dict) else None
    return f"/api/v1/recordings/{rec['id']}" if isinstance(rec, dict) and rec.get("id") else None


@router.put("/uploads/{token}", status_code=201, include_in_schema=False)
async def receive_upload(token: str, request: Request) -> Response:
    """The local storage adapter's pre-signed PUT target (§11.4): authorized by the signed token,
    not by a session. Not part of a hosted deployment, where the browser uploads to the bucket."""
    storage = storage_of(request)
    if not isinstance(storage, LocalObjectStorage):
        return problem_response(request, status=404, code="not_found", title="Not found")
    length = request.headers.get("content-length")
    try:
        grant = storage.signer.verify(token, now=now())
        if length is not None and length.isdigit() and int(length) > grant.max_bytes:
            return problem_response(request, status=413, code="too_large", title="The upload is too large")
        await storage.receive(
            token, content_type=request.headers.get("content-type"), chunks=request.stream(), now=now()
        )
    except ValidationFailed as exc:
        if exc.details.get("reason") == "too_large":
            return problem_response(request, status=413, code="too_large", title="The upload is too large")
        return problem_response(
            request,
            status=415,
            code="unsupported_media_type",
            title="Unsupported media type",
            detail=exc.message,
        )
    return Response(status_code=201)


@router.post("/recordings/{recording_id}/complete", status_code=202)
async def complete_recording(
    recording_id: UUID, request: Request, user: User, factory: Factory
) -> JSONResponse:
    storage = storage_of(request)
    async with factory(user_id=user.user_id) as uow:
        r = await meetings.complete_upload(uow, storage, recording_id, now=now())
    return JSONResponse(
        recording_json(r), status_code=202, headers={"Location": f"/api/v1/recordings/{recording_id}"}
    )


@router.get("/recordings")
async def list_recordings(
    user: User, factory: Factory, codec: Cursors, cursor: str | None = None, limit: int | None = None
) -> dict[str, Any]:
    scope = "recordings"
    async with factory(user_id=user.user_id) as uow:
        rows, next_key = await meetings.list_recordings_page(
            uow, after=codec.decode(cursor, user_id=user.user_id, scope=scope), limit=limit_of(limit)
        )
    return page_body([recording_json(r) for r in rows], next_key, codec=codec, user=user, scope=scope)


@router.get("/recordings/meeting-suggestions")
async def recording_meeting_suggestions(
    user: User, factory: Factory, occurred_at: datetime.datetime | None = None
) -> dict[str, Any]:
    """The upload dialog's suggestions: overlapping or recent calendar meetings (§16.9)."""
    async with factory(user_id=user.user_id) as uow:
        rows = await meetings.meeting_suggestions(uow, occurred_at=occurred_at, now=now())
    return {"items": [as_json(s) for s in rows]}


@router.get("/recordings/{recording_id}")
async def get_recording(recording_id: UUID, user: User, factory: Factory) -> dict[str, Any]:
    async with factory(user_id=user.user_id) as uow:
        r = await meetings.get_recording(uow, recording_id)
    return recording_json(r)


@router.post("/recordings/{recording_id}/retry", status_code=202)
async def retry_recording(recording_id: UUID, user: User, factory: Factory) -> JSONResponse:
    async with factory(user_id=user.user_id) as uow:
        r = await meetings.retry_recording(uow, recording_id)
    return JSONResponse(recording_json(r), status_code=202)


class LinkMeeting(BaseModel):
    model_config = ConfigDict(extra="forbid")
    meeting_id: UUID | None


@router.put("/recordings/{recording_id}/meeting")
async def link_recording(
    recording_id: UUID, body: LinkMeeting, user: User, factory: Factory
) -> dict[str, Any]:
    async with factory(user_id=user.user_id) as uow:
        r = await meetings.link_meeting(uow, recording_id, body.meeting_id)
    return recording_json(r)


# ---------------------------------------------------------------- meeting page (slice 4.3)

YOURS = frozenset({"my_commitment", "my_task"})
THEIRS = frozenset({"waiting_for", "delegated"})


def _evidence(rows: list[work.EvidenceView]) -> list[dict[str, Any]]:
    return [{**evidence_json(e), "start_ms": e.start_ms, "end_ms": e.end_ms} for e in rows]


def summary_json(m: meetings.MeetingRecord) -> dict[str, Any] | None:
    """The AI-10 summary is an inference: labelled, with origin, status and provenance."""
    if not m.summary:
        return None
    return as_json(
        {
            "label": "AI summary",
            "text": m.summary.get("text") or "",
            "topics": m.summary.get("topics") or [],
            "concerns": m.summary.get("concerns") or [],
            "origin": "ai",
            "verification_status": "suggested",
            "provenance": {
                "source": "ai_inference",
                "extraction_method": "llm",
                "extraction_id": m.summary_extraction_id,
                "model": m.summary_model,
                "prompt_version": m.summary_prompt_version,
                "derived_at": m.summary_derived_at,
            },
        }
    )


async def _speakers(uow: UnitOfWork, meeting_id: UUID) -> list[dict[str, Any]]:
    mappings = await meetings.speaker_mappings(uow, meeting_id)
    labels = await meetings.transcript_labels(uow, meeting_id)
    refs = await people.get_persons(uow, [m.person_id for m in mappings])
    out: list[dict[str, Any]] = []
    for label in labels:
        owner = next((m for m in mappings if label in m.labels), None)
        ref = refs.get(owner.person_id) if owner else None
        out.append(
            as_json(
                {
                    "label": label,
                    "person_id": owner.person_id if owner else None,
                    "person_name": (ref.display_name or ref.primary_email) if ref else None,
                    "status": owner.status if owner else "unmapped",
                    "origin": owner.origin if owner else None,
                    "method": owner.method if owner else None,
                    "confidence": owner.confidence if owner else None,
                    "model": owner.model if owner else None,
                    "derived_at": owner.derived_at if owner else None,
                    "confirmed_at": owner.confirmed_at if owner else None,
                }
            )
        )
    return out


async def _what_changed(uow: UnitOfWork, m: meetings.MeetingRecord, at: datetime.datetime) -> dict[str, Any]:
    prior = await meetings.prior_meetings(uow, m.id, limit=1)
    if not prior:
        return {"previous_meeting": None, "changes": [], "note": "No previous related meeting."}
    previous = prior[0]
    self_p = await people.get_self_person(uow)
    sources = [m.source_item_id, previous.source_item_id]
    for mid in (m.id, previous.id):
        rec = await meetings.recording_for_meeting(uow, mid)
        if rec is not None and rec.source_item_id is not None:
            sources.append(rec.source_item_id)
    changes = await work.changes_between(
        uow,
        since=previous.ends_at,
        until=min(m.ends_at, at),
        person_ids=set(m.participant_ids) - {self_p.id},
        source_item_ids=sources,
        meeting_ids=[m.id, previous.id],
    )
    items = {
        v.id: v
        for v in await work.items_by_ids(uow, [c.entity_id for c in changes if c.entity_type == "work_item"])
    }
    decisions = {
        d.id: d
        for d in await work.decisions_by_ids(
            uow, [c.entity_id for c in changes if c.entity_type == "decision"]
        )
    }
    rows = []
    for c in changes:
        title = (
            items[c.entity_id].title
            if c.entity_id in items
            else (decisions[c.entity_id].statement if c.entity_id in decisions else None)
        )
        rows.append(
            as_json(
                {
                    "entity_type": c.entity_type,
                    "entity_id": c.entity_id,
                    "kind": c.kind,
                    "title": title,
                    "before": c.before,
                    "after": c.after,
                    "materiality": c.materiality,
                    "recorded_at": c.recorded_at,
                }
            )
        )
    return {
        "previous_meeting": as_json(
            {
                "id": previous.id,
                "title": previous.title,
                "starts_at": previous.starts_at,
                "ends_at": previous.ends_at,
            }
        ),
        "changes": rows,
    }


async def meeting_page(uow: UnitOfWork, meeting_id: UUID, *, at: datetime.datetime) -> dict[str, Any]:
    m = await meetings.meeting_record(uow, meeting_id)
    if m is None:
        raise NotFound("meeting not found")
    rec = await meetings.recording_for_meeting(uow, meeting_id)
    mw = await work.meeting_work(
        uow, meeting_id=meeting_id, source_item_id=rec.source_item_id if rec else None
    )
    refs = await people.get_persons(uow, m.participant_ids)
    groups: dict[str, list[dict[str, Any]]] = {"yours": [], "theirs": [], "others": [], "unresolved": []}
    for item in mw.items:
        key = (
            "yours"
            if item.direction in YOURS
            else "theirs"
            if item.direction in THEIRS
            else "unresolved"
            if item.direction == "unresolved"
            else "others"
        )
        groups[key].append({**item_json(item), "evidence": _evidence(mw.evidence.get(item.id, []))})
    if m.summary:
        summary_status = "ready"
    elif rec is None:
        summary_status = "none"
    elif rec.status in ("failed", "rejected"):
        summary_status = "failed"
    else:
        summary_status = "pending"
    return {
        "meeting": as_json(
            {
                "id": m.id,
                "title": m.title,
                "description": m.description,
                "starts_at": m.starts_at,
                "ends_at": m.ends_at,
                "timezone": m.timezone,
                "status": m.status,
                "origin": m.origin,
                "processing_status": m.processing_status,
                "conference_uri": m.conference_uri,
                "version": m.version,
                "participants": [
                    {
                        "person_id": pid,
                        "name": (refs[pid].display_name or refs[pid].primary_email) if pid in refs else None,
                        "is_self": refs[pid].is_self if pid in refs else False,
                    }
                    for pid in m.participant_ids
                ],
            }
        ),
        "recording": recording_json(rec) if rec else None,
        "summary": summary_json(m),
        "summary_status": summary_status,
        "decisions": [
            {**decision_json(d), "evidence": _evidence(mw.evidence.get(d.id, []))} for d in mw.decisions
        ],
        "open_questions": [
            {**decision_json(d), "evidence": _evidence(mw.evidence.get(d.id, []))} for d in mw.open_questions
        ],
        "items": groups,
        "speakers": await _speakers(uow, meeting_id),
        "what_changed": await _what_changed(uow, m, at),
    }


@router.get("/meetings/{meeting_id}")
async def get_meeting(meeting_id: UUID, user: User, factory: Factory) -> JSONResponse:
    """The meeting page and the missed-meeting view (PRD §22, §25): deterministic reads only."""
    async with factory(user_id=user.user_id) as uow:
        body = await meeting_page(uow, meeting_id, at=now())
    return JSONResponse(body, headers={"ETag": etag(int(body["meeting"]["version"]))})


class SpeakerMappingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=60)
    person_id: UUID | None


class SpeakersIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mappings: list[SpeakerMappingIn] = Field(min_length=1, max_length=40)
    base_version: int | None = None


@router.put("/meetings/{meeting_id}/speakers")
async def put_speakers(
    meeting_id: UUID,
    body: SpeakersIn,
    user: User,
    factory: Factory,
    if_match: IfMatch = None,
    idempotency_key: IdempotencyKey = None,
) -> JSONResponse:
    """The user's speaker mapping (authority 5): user mappings survive re-extraction; items whose
    statements came from a remapped label are re-pointed as ``actor = user`` events."""
    labels = [m.label for m in body.mappings]
    if len(set(labels)) != len(labels):
        raise ValidationFailed("each label may appear once")
    base = parse_if_match(if_match) if if_match is not None else body.base_version
    key = request_key(idempotency_key)
    at = now()
    async with factory(user_id=user.user_id) as uow:
        wanted = [m.person_id for m in body.mappings if m.person_id is not None]
        known = set(await people.get_persons(uow, wanted))
        before, after = await meetings.set_speaker_mapping(
            uow,
            meeting_id,
            [(m.label, m.person_id) for m in body.mappings],
            base_version=base,
            known_persons=known,
            now=at,
        )
        await work.remap_speaker_items(uow, meeting_id, before=before, after=after, request_key=key, now=at)
        for m in body.mappings:
            await record_feedback(
                uow,
                target_type="speaker_mapping",
                target_id=meeting_id,
                action="speaker_confirmed" if m.person_id else "speaker_none_of_these",
                before={"label": m.label, "person_id": str(before[m.label]) if m.label in before else None},
                after={"label": m.label, "person_id": str(m.person_id) if m.person_id else None},
            )
        await work.record_entity_event(
            uow,
            entity_type="meeting",
            entity_id=meeting_id,
            event_type="speakers_confirmed",
            actor="user",
            authority=5,
            materiality=2,
            occurred_at=at,
            dedupe_key=work.user_dedupe_key(key, "speakers_confirmed", meeting_id),
            payload={"labels": labels},
        )
        speakers = await _speakers(uow, meeting_id)
        record = await meetings.meeting_record(uow, meeting_id)
        assert record is not None
    return JSONResponse(
        {"speakers": speakers, "version": record.version}, headers={"ETag": etag(record.version)}
    )


# ---------------------------------------------------------------- prep (slice 4.4)


def _asks_json(asks: dict[str, Any]) -> dict[str, Any]:
    """AI-11 asks are recommendations: labelled, with provenance, never phrased as fact."""
    return {
        "version": asks.get("version"),
        "status": asks.get("status", "not_requested"),
        "label": "AI suggestions",
        "items": asks.get("items") or [],
        "provenance": asks.get("provenance"),
    }


async def _prep_body(uow: UnitOfWork, meeting_id: UUID) -> dict[str, Any]:
    """Sections computed at read time; asks only when they belong to the current key."""
    view = await attention.compute_prep(uow, meeting_id, now=now())
    if view is None:
        raise NotFound("meeting not found")
    stored = await meetings.get_prep(uow, meeting_id)
    current = stored is not None and stored.key == view.key
    asks = stored.asks if current and stored is not None else {"status": "not_requested", "items": []}
    return {
        "meeting_id": str(meeting_id),
        "key": view.key,
        "version": stored.version if current and stored is not None else None,
        "empty": view.empty,
        "sections": view.sections,
        "asks": _asks_json(asks),
    }


@router.get("/meetings/{meeting_id}/prep")
async def get_meeting_prep(meeting_id: UUID, user: User, factory: Factory) -> dict[str, Any]:
    """The prep view (BACKEND_DESIGN.md §16.9): deterministic sections, no model call, no write."""
    async with factory(user_id=user.user_id) as uow:
        return await _prep_body(uow, meeting_id)


@router.post("/meetings/{meeting_id}/prep/asks", status_code=202)
async def request_meeting_asks(meeting_id: UUID, user: User, factory: Factory) -> JSONResponse:
    """The web app calls this when the prep view opens with non-empty sections: the sections are
    stored for the current key and AI-11 is requested once per version (202), or the stored asks
    are returned (200). 409 ``nothing_to_ask`` when the sections are empty (no AI-11 call)."""
    at = now()
    async with factory(user_id=user.user_id) as uow:
        view = await attention.compute_prep(uow, meeting_id, now=at)
        if view is None:
            raise NotFound("meeting not found")
        if view.empty:
            raise Conflict(
                "there is nothing to prepare for this meeting", details={"reason": "nothing_to_ask"}
            )
        version = await attention.store_prep(uow, view, now=at)
        prep = await meetings.request_asks(uow, meeting_id, version=version)
        body = await _prep_body(uow, meeting_id)
    status = 202 if prep is None or prep.asks["status"] == "pending" else 200
    return JSONResponse(body, status_code=status)
