"""Conversations, people, organizations, meetings, sources, Today and "Your data" routes
(slices 1.7-1.9, BACKEND_DESIGN.md §16.5)."""

from __future__ import annotations

import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from eca import attention, communication, ingestion, meetings, people, privacy, work
from eca.api.common import (
    Cursors,
    Factory,
    IfMatch,
    User,
    as_json,
    etag,
    limit_of,
    now,
    page_body,
    parse_if_match,
)
from eca.api.work import item_json
from eca.platform.errors import NotFound, ValidationFailed
from eca.platform.feedback import record_feedback

router = APIRouter(prefix="/api/v1")


# --- conversations ----------------------------------------------------------------------------


def conversation_json(c: communication.ConversationSummary) -> dict[str, Any]:
    reasons = {"triage": "ai_triage", "heuristic": "unanswered_inbound", "user": "user"}
    reason = reasons.get(c.needs_reply_source or "")
    return as_json(
        {
            "id": c.id,
            "subject": c.subject,
            "awaiting": c.awaiting,
            "needs_reply": c.needs_reply,
            "needs_reply_reason": reason,
            "last_message_at": c.last_message_at,
            "last_inbound_at": c.last_inbound_at,
            "handled_by_user_at": c.handled_by_user_at,
            "priority": {
                "score": c.priority_score,
                "reasons": c.priority_reasons,
                "override": c.priority_override,
            },
            "latest_snippet": c.latest_snippet,
            # AI-01 triage is an inference: labelled, never presented as fact (§16.1).
            "triage": {"origin": "ai", "verification_status": "suggested", **c.latest_triage}
            if c.latest_triage
            else None,
            "version": c.version,
        }
    )


@router.get("/conversations")
async def list_conversations(
    user: User,
    factory: Factory,
    codec: Cursors,
    awaiting: Literal["user"] = "user",
    cursor: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    scope = f"conversations:{awaiting}"
    async with factory(user_id=user.user_id) as uow:
        page = await communication.needs_response_page(
            uow, after=codec.decode(cursor, user_id=user.user_id, scope=scope), limit=limit_of(limit)
        )
    return page_body(
        [conversation_json(c) for c in page.items], page.next_key, codec=codec, user=user, scope=scope
    )


@router.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: UUID, user: User, factory: Factory) -> JSONResponse:
    async with factory(user_id=user.user_id) as uow:
        conv, messages = await communication.conversation_detail(uow, conversation_id)
        persons = await people.get_persons(
            uow, sorted({m.sender_person_id for m in messages if m.sender_person_id})
        )
    body = conversation_json(conv) | {
        "messages": jsonable_encoder(messages),
        "people": {str(k): jsonable_encoder(v) for k, v in persons.items()},
    }
    return JSONResponse(body, headers={"ETag": etag(conv.version)})


@router.post("/conversations/{conversation_id}/mark-handled", status_code=204)
async def mark_handled(conversation_id: UUID, user: User, factory: Factory) -> Response:
    async with factory(user_id=user.user_id) as uow:
        await communication.mark_handled(uow, conversation_id, at=now())
    return Response(status_code=204)


class PatchConversation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    priority_override: Literal[-1, 0, 1] | None


@router.patch("/conversations/{conversation_id}")
async def patch_conversation(
    conversation_id: UUID, body: PatchConversation, user: User, factory: Factory, if_match: IfMatch = None
) -> JSONResponse:
    async with factory(user_id=user.user_id) as uow:
        conv = await communication.set_priority_override(
            uow, conversation_id, body.priority_override, if_match=parse_if_match(if_match)
        )
    return JSONResponse(conversation_json(conv), headers={"ETag": etag(conv.version)})


# --- people and organizations -------------------------------------------------------------------


@router.get("/people")
async def list_people(
    user: User,
    factory: Factory,
    codec: Cursors,
    q: str | None = None,
    sort: Literal["importance", "recent"] = "importance",
    cursor: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    scope = f"people:{q}:{sort}"
    async with factory(user_id=user.user_id) as uow:
        items, next_key = await people.list_people_page(
            uow,
            q=q,
            sort=sort,
            after=codec.decode(cursor, user_id=user.user_id, scope=scope),
            limit=limit_of(limit),
        )
    return page_body(items, next_key, codec=codec, user=user, scope=scope)


@router.get("/people/{person_id}")
async def get_person(person_id: UUID, user: User, factory: Factory) -> JSONResponse:
    """Person context: profile, identifiers and the open work involving them in both directions."""
    async with factory(user_id=user.user_id) as uow:
        detail = await people.person_detail(uow, person_id)
        involved = await work.list_items_page(uow, person_id=detail.person.id, sort="due", limit=50)
    body = jsonable_encoder(detail) | {
        "they_owe_me": [item_json(i) for i in involved.items if i.direction in ("waiting_for", "delegated")],
        "i_owe_them": [item_json(i) for i in involved.items if i.direction in ("my_commitment", "my_task")],
        "other_items": [
            item_json(i)
            for i in involved.items
            if i.direction not in ("waiting_for", "delegated", "my_commitment", "my_task")
        ],
    }
    return JSONResponse(body, headers={"ETag": etag(detail.person.version)})


class PatchPerson(BaseModel):
    model_config = ConfigDict(extra="forbid")
    importance_user: int | None = Field(default=None, ge=1, le=5)
    role_title: str | None = Field(default=None, max_length=200)
    display_name: str | None = Field(default=None, min_length=1, max_length=200)


@router.patch("/people/{person_id}")
async def patch_person(
    person_id: UUID, body: PatchPerson, user: User, factory: Factory, if_match: IfMatch = None
) -> JSONResponse:
    async with factory(user_id=user.user_id) as uow:
        p = await people.edit_person(
            uow, person_id, body.model_dump(exclude_unset=True), if_match=parse_if_match(if_match)
        )
    return JSONResponse(jsonable_encoder(p), headers={"ETag": etag(p.version)})


class MergePerson(BaseModel):
    model_config = ConfigDict(extra="forbid")
    into_id: UUID


@router.post("/people/{person_id}/merge", status_code=204)
async def merge_person(person_id: UUID, body: MergePerson, user: User, factory: Factory) -> Response:
    async with factory(user_id=user.user_id) as uow:
        await people.merge_persons(uow, source_id=person_id, target_id=body.into_id)
        await record_feedback(
            uow,
            target_type="person",
            target_id=person_id,
            action="merge",
            after={"into_id": str(body.into_id)},
        )
    return Response(status_code=204)


class AliasBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["email", "name"]
    value: str = Field(min_length=1, max_length=320)


@router.post("/people/{person_id}/aliases", status_code=204)
async def add_alias(person_id: UUID, body: AliasBody, user: User, factory: Factory) -> Response:
    async with factory(user_id=user.user_id) as uow:
        await people.add_alias(uow, person_id, kind=body.kind, value=body.value)
    return Response(status_code=204)


@router.get("/organizations")
async def list_organizations(user: User, factory: Factory) -> dict[str, Any]:
    async with factory(user_id=user.user_id) as uow:
        orgs = await people.list_organizations(uow)
    return {"items": jsonable_encoder(orgs), "next_cursor": None, "total": len(orgs)}


class PatchOrganization(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=200)
    importance_user: int | None = Field(default=None, ge=1, le=5)


@router.patch("/organizations/{org_id}")
async def patch_organization(org_id: UUID, body: PatchOrganization, user: User, factory: Factory) -> Any:
    async with factory(user_id=user.user_id) as uow:
        org = await people.edit_organization(uow, org_id, body.model_dump(exclude_unset=True))
    return jsonable_encoder(org)


# --- meetings and sources -------------------------------------------------------------------------


@router.get("/meetings")
async def list_meetings(
    user: User,
    factory: Factory,
    from_: Annotated[datetime.datetime | None, Query(alias="from")] = None,
    to: datetime.datetime | None = None,
) -> dict[str, Any]:
    start = from_ or now()
    end = to or start + datetime.timedelta(days=7)
    if end <= start or end - start > datetime.timedelta(days=62):
        raise ValidationFailed("the range must be positive and at most 62 days")
    async with factory(user_id=user.user_id) as uow:
        rows = await meetings.meetings_between(uow, start, end, include_cancelled=True)
    return {"items": jsonable_encoder(rows), "next_cursor": None}


@router.get("/sources/{source_item_id}")
async def get_source(source_item_id: UUID, user: User, factory: Factory) -> dict[str, Any]:
    """Source view within retention: metadata always, message text only while it is retained."""
    async with factory(user_id=user.user_id) as uow:
        item = await ingestion.get_source_item(uow, source_item_id)
        body: dict[str, Any] = {
            "id": str(item.id),
            "kind": item.kind,
            "provider": item.provider,
            "occurred_at": item.occurred_at.isoformat(),
            "deleted": item.content is None,
            "deep_link": _deep_link(item.provider, item.kind, item.external_id, item.external_thread_id),
        }
        if item.kind == "message" and item.content is not None:
            try:
                view = await communication.get_message_view(uow, source_item_id)
            except Exception as exc:
                raise NotFound("message view not available") from exc
            body |= {
                "subject": view.subject,
                "sent_at": view.sent_at.isoformat(),
                "direction": view.direction,
                "sender": jsonable_encoder(view.sender),
                "text": view.body_clean or None,
                "body_available": bool(view.body_clean),
            }
    return body


def _deep_link(provider: str, kind: str, external_id: str, thread_id: str | None) -> str | None:
    if provider == "gmail" and kind == "message":
        return f"https://mail.google.com/mail/u/0/#all/{thread_id or external_id}"
    return None


# --- Today and privacy --------------------------------------------------------------------------


@router.get("/today")
async def today(user: User, factory: Factory) -> dict[str, Any]:
    async with factory(user_id=user.user_id) as uow:
        t = await attention.build_today(uow, now=now())
        conversations = {c.id: c for c in t.needs_response}
    attention_rows = []
    for a in t.attention:
        if a["kind"] == "work_item" and a["id"] in t.attention_items:
            attention_rows.append({"kind": "work_item", "item": item_json(t.attention_items[a["id"]])})
        elif a["kind"] == "conversation" and a["id"] in conversations:
            attention_rows.append(
                {"kind": "conversation", "conversation": conversation_json(conversations[a["id"]])}
            )
    return {
        "date": t.date,
        "timezone": t.timezone,
        "attention": attention_rows,
        "commitments": [item_json(i) for i in t.commitments],
        "waiting_for": [item_json(i) for i in t.waiting_for],
        "deadlines": [item_json(i) for i in t.deadlines],
        "new_suggestions": [item_json(i) for i in t.new_suggestions],
        "needs_response": [conversation_json(c) for c in t.needs_response],
        "meetings": jsonable_encoder(t.meetings),
        "people": {str(k): jsonable_encoder(v) for k, v in t.people.items()},
    }


@router.get("/data-summary")
async def data_summary(user: User, factory: Factory) -> dict[str, Any]:
    async with factory(user_id=user.user_id) as uow:
        summary = await privacy.data_summary(uow)
    return as_json(summary)
