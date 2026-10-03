"""Work items, decisions and evidence routes (slice 1.7, BACKEND_DESIGN.md §16.5).

Every AI-derived resource carries ``origin``, ``verification_status`` and a ``provenance``
envelope (§16.1). Item reads return an ``ETag`` (the version); PATCH takes ``If-Match`` (412 when
stale) or ``base_version`` in the body (field-level 409).
"""

from __future__ import annotations

import datetime
import json
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from eca import attention, work
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
from eca.platform.errors import ValidationFailed
from eca.platform.uow import UnitOfWork

router = APIRouter(prefix="/api/v1")

Direction = Literal[
    "my_task", "my_commitment", "delegated", "waiting_for", "shared", "observed", "unresolved"
]
ItemType = Literal["task", "commitment", "request", "follow_up", "deadline"]


def provenance(v: Any, evidence_source_ids: tuple[UUID, ...] = ()) -> dict[str, Any]:
    return {
        "source": "user" if v.origin == "user" else "ai_inference",
        "confidence": v.confidence,
        "confidence_band": v.confidence_band,
        "derived_at": v.derived_at,
        "extraction_method": v.extraction_method,
        "extraction_id": v.extraction_id,
        "model": v.model,
        "evidence_source_ids": list(evidence_source_ids),
    }


def item_json(v: work.WorkItemView) -> dict[str, Any]:
    return as_json(
        {
            "id": v.id,
            "type": v.type,
            "title": v.title,
            "direction": v.direction,
            "owner_person_id": v.owner_person_id,
            "counterparty_person_id": v.counterparty_person_id,
            "requester_person_id": v.requester_person_id,
            "due_at": v.due_at,
            "due_precision": v.due_precision,
            "due_text": v.due_text,
            "lifecycle_status": v.lifecycle_status,
            "verification_status": v.verification_status,
            "origin": v.origin,
            "commitment_strength": v.commitment_strength,
            "statement_kind": v.statement_kind,
            "reported_status": v.reported_status,
            "user_fields": list(v.user_fields),
            "has_conflict": v.has_conflict,
            "has_source_gap": v.has_source_gap,
            "pending_adjudication": v.pending_adjudication,
            "notes": v.notes,
            "priority": {"score": v.priority_score, "reasons": list(v.priority_reasons)},
            "version": v.version,
            "provenance": provenance(v, v.evidence_source_ids),
        }
    )


def evidence_json(e: work.EvidenceView) -> dict[str, Any]:
    return as_json(
        {
            "id": e.id,
            "source_item_id": e.source_item_id,
            "quote": e.quote,
            "relation": e.relation,
            "extraction_id": e.extraction_id,
            "occurred_at": e.occurred_at,
            "redacted": e.quote == work.REDACTED,
        }
    )


async def _detail_response(uow: UnitOfWork, item_id: UUID, *, status: int = 200) -> JSONResponse:
    d = await work.item_detail(uow, item_id)
    body = item_json(d.item) | {
        "conflicts": jsonable_encoder(d.conflicts),
        "evidence": [evidence_json(e) for e in d.evidence],
        "timeline": jsonable_encoder(d.timeline),
    }
    return JSONResponse(body, status_code=status, headers={"ETag": etag(d.item.version)})


@router.get("/work-items")
async def list_work_items(
    user: User,
    factory: Factory,
    codec: Cursors,
    direction: Direction | None = None,
    status: Literal["open", "closed", "all"] = "open",
    verification: Literal["suggested", "confirmed", "rejected", "user_created"] | None = None,
    person_id: UUID | None = None,
    due_before: datetime.datetime | None = None,
    sort: Literal["due", "priority", "created"] = "due",
    cursor: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    scope = f"work-items:{direction}:{status}:{verification}:{person_id}:{due_before}:{sort}"
    async with factory(user_id=user.user_id) as uow:
        page = await work.list_items_page(
            uow,
            direction=direction,
            status=status,
            verification=verification,
            person_id=person_id,
            due_before=due_before,
            sort=sort,
            after=codec.decode(cursor, user_id=user.user_id, scope=scope),
            limit=limit_of(limit),
        )
    return page_body([item_json(i) for i in page.items], page.next_key, codec=codec, user=user, scope=scope)


class CreateItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=300)
    type: ItemType = "task"
    direction: Direction = "my_task"
    owner_person_id: UUID | None = None
    due_at: datetime.datetime | None = None


@router.post("/work-items", status_code=201)
async def create_work_item(
    body: CreateItem, request: Request, user: User, factory: Factory, idempotency_key: IdempotencyKey = None
) -> JSONResponse:
    async def run(uow: UnitOfWork) -> tuple[int, dict[str, Any]]:
        item_id = await work.create_item_for_user(
            uow,
            title=body.title,
            type_=body.type,
            direction=body.direction,
            owner_person_id=body.owner_person_id,
            due_at=body.due_at,
            request_key=request_key(idempotency_key),
            at=now(),
        )
        return 201, item_json(await work.get_item(uow, item_id))

    resp = await idempotent(factory, user, request, idempotency_key, body.model_dump(mode="json"), run)
    if resp.status_code == 201:
        resp.headers["Location"] = f"/api/v1/work-items/{json.loads(bytes(resp.body))['id']}"
    return resp


@router.get("/work-items/{item_id}")
async def get_work_item(item_id: UUID, user: User, factory: Factory) -> JSONResponse:
    async with factory(user_id=user.user_id) as uow:
        return await _detail_response(uow, item_id)


class PatchItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_version: int | None = None
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = None
    type: ItemType | None = None
    owner_person_id: UUID | None = None
    counterparty_person_id: UUID | None = None
    due_at: datetime.datetime | None = None
    due_precision: Literal["datetime", "day", "week", "fuzzy"] | None = None
    due_text: str | None = None
    project_hint: str | None = None
    notes: str | None = None
    # User priority (slice 3.5): 1 pins high, -1 pins low, 0/null clears; records preference pairs.
    priority_override: Literal[-1, 0, 1] | None = None


@router.patch("/work-items/{item_id}")
async def patch_work_item(
    item_id: UUID,
    body: PatchItem,
    user: User,
    factory: Factory,
    if_match: IfMatch = None,
    idempotency_key: IdempotencyKey = None,
) -> JSONResponse:
    changes = body.model_dump(exclude_unset=True)
    base_version = changes.pop("base_version", None)
    has_override = "priority_override" in changes
    override = changes.pop("priority_override", None)
    if not changes and not has_override:
        raise ValidationFailed("no fields to change")
    at = now()
    async with factory(user_id=user.user_id) as uow:
        expected = parse_if_match(if_match)
        if changes:
            result = await work.edit_item(
                uow,
                item_id,
                changes,
                request_key=request_key(idempotency_key),
                at=at,
                if_match=expected,
                base_version=base_version,
            )
            expected = result.version
        if has_override:
            # Not a folded field: a USER-AUTHORED column with its own user event (TECHNICAL_DESIGN.md §12.8).
            await work.set_item_priority_override(
                uow, item_id, override, if_match=expected, request_key=request_key(idempotency_key), at=at
            )
            await attention.record_override_pairs(
                uow,
                attention.priority_config(),
                entity_type="work_item",
                entity_id=item_id,
                override=override,
                now=at,
            )
        return await _detail_response(uow, item_id)


@router.delete("/work-items/{item_id}", status_code=204)
async def delete_work_item(item_id: UUID, user: User, factory: Factory) -> Response:
    async with factory(user_id=user.user_id) as uow:
        await work.delete_user_item(uow, item_id, at=now())
    return Response(status_code=204)


class MergeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    into_id: UUID


@router.post("/work-items/{item_id}/merge")
async def merge_work_item(
    item_id: UUID, body: MergeBody, user: User, factory: Factory, idempotency_key: IdempotencyKey = None
) -> JSONResponse:
    async with factory(user_id=user.user_id) as uow:
        await work.merge_items(uow, item_id, body.into_id, request_key=request_key(idempotency_key), at=now())
        return await _detail_response(uow, body.into_id)


@router.post("/work-items/{item_id}/{command}")
async def work_item_command(
    item_id: UUID,
    command: Literal["confirm", "reject", "complete", "reopen", "cancel", "start"],
    user: User,
    factory: Factory,
    idempotency_key: IdempotencyKey = None,
) -> JSONResponse:
    key = request_key(idempotency_key)
    async with factory(user_id=user.user_id) as uow:
        if command in ("confirm", "reject"):
            await work.verify_item(uow, item_id, command, request_key=key, at=now())
        else:
            await work.lifecycle_command(uow, item_id, command, request_key=key, at=now())
        return await _detail_response(uow, item_id)


# --- decisions ------------------------------------------------------------------------------


def decision_json(d: work.DecisionView) -> dict[str, Any]:
    return as_json(
        {
            "id": d.id,
            "kind": d.kind,
            "statement": d.statement,
            "rationale": d.rationale,
            "decided_at": d.decided_at,
            "conversation_id": d.conversation_id,
            "project_hint": d.project_hint,
            "superseded_by_id": d.superseded_by_id,
            "resolved_by_id": d.resolved_by_id,
            "origin": d.origin,
            "verification_status": d.verification_status,
            "notes": d.notes,
            "user_fields": list(d.user_fields),
            "version": d.version,
            "created_at": d.created_at,
            "provenance": provenance(d),
        }
    )


@router.get("/decisions")
async def list_decisions(
    user: User,
    factory: Factory,
    codec: Cursors,
    kind: Literal["decision", "open_question"] | None = None,
    since: datetime.datetime | None = None,
    cursor: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    scope = f"decisions:{kind}:{since}"
    async with factory(user_id=user.user_id) as uow:
        page = await work.list_decisions_page(
            uow,
            kind=kind,
            since=since,
            after=codec.decode(cursor, user_id=user.user_id, scope=scope),
            limit=limit_of(limit),
        )
    return page_body(
        [decision_json(d) for d in page.items], page.next_key, codec=codec, user=user, scope=scope
    )


async def _decision_response(uow: UnitOfWork, decision: work.DecisionView) -> JSONResponse:
    body = decision_json(decision) | {
        "evidence": [evidence_json(e) for e in await work.evidence_of(uow, "decision", decision.id)]
    }
    return JSONResponse(body, headers={"ETag": etag(decision.version)})


@router.get("/decisions/{decision_id}")
async def get_decision(decision_id: UUID, user: User, factory: Factory) -> JSONResponse:
    async with factory(user_id=user.user_id) as uow:
        return await _decision_response(uow, await work.get_decision(uow, decision_id))


class PatchDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    statement: str | None = Field(default=None, min_length=1, max_length=1000)
    rationale: str | None = None
    decided_at: datetime.datetime | None = None
    project_hint: str | None = None
    notes: str | None = None
    kind: Literal["decision", "open_question"] | None = None


@router.patch("/decisions/{decision_id}")
async def patch_decision(
    decision_id: UUID, body: PatchDecision, user: User, factory: Factory, if_match: IfMatch = None
) -> JSONResponse:
    async with factory(user_id=user.user_id) as uow:
        d = await work.edit_decision(
            uow, decision_id, body.model_dump(exclude_unset=True), if_match=parse_if_match(if_match)
        )
        return await _decision_response(uow, d)


@router.post("/decisions/{decision_id}/{command}")
async def decision_command(
    decision_id: UUID, command: Literal["confirm", "reject", "resolve"], user: User, factory: Factory
) -> JSONResponse:
    async with factory(user_id=user.user_id) as uow:
        return await _decision_response(uow, await work.decision_command(uow, decision_id, command, at=now()))


@router.get("/evidence/{evidence_id}")
async def get_evidence(evidence_id: UUID, user: User, factory: Factory) -> dict[str, Any]:
    async with factory(user_id=user.user_id) as uow:
        return evidence_json(await work.get_evidence(uow, evidence_id))
