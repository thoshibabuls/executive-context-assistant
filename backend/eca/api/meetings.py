"""Recording and meeting routes (Phase 4, BACKEND_DESIGN.md §11.4, §16.9).

Slice 4.1: upload init (``Idempotency-Key`` required, 10 per hour, sha256 dedupe), the local
storage adapter's signed PUT target, complete, status, list, retry, meeting link and the upload
dialog's meeting suggestions. Statuses and limits are decided in ``eca.meetings``; this module only
maps them to HTTP.
"""

from __future__ import annotations

import datetime
import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from eca import meetings
from eca.api.chat import _limiter as limiter
from eca.api.common import (
    Cursors,
    Factory,
    IdempotencyKey,
    User,
    as_json,
    idempotent,
    limit_of,
    now,
    page_body,
)
from eca.api.problems import problem_response
from eca.platform.errors import UpstreamUnavailable, ValidationFailed
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
