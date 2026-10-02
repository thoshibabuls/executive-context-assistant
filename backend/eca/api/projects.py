"""Project and topic-mode routes (slice 2.5, BACKEND_DESIGN.md §16.5, §16.7).

Suggestions carry ``origin = ai`` and ``verification_status = suggested`` until the user confirms
them; confirmation, rejection, edits and item assignment are authority-5 user actions. Topic mode
groups are labelled "grouped by topic; not a confirmed project".
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from eca import projects, retrieval
from eca.api.assistant import _card_json
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
from eca.intelligence import AIClient
from eca.platform.errors import ValidationFailed
from eca.platform.uow import UnitOfWork

router = APIRouter(prefix="/api/v1")


def project_json(p: projects.ProjectView) -> dict[str, Any]:
    return as_json(
        {
            "id": p.id,
            "name": p.name,
            "description": p.description,
            "aliases": list(p.aliases),
            "status": p.status,
            "importance_user": p.importance_user,
            "origin": p.origin,
            "verification_status": p.verification_status,
            "label": "AI suggestion" if p.verification_status == "suggested" else None,
            "suggestion_sources": p.suggestion_sources,
            "user_fields": list(p.user_fields),
            "version": p.version,
            "provenance": {
                "source": "user" if p.origin == "user" or p.confirmed else "ai_inference",
                "extraction_method": "user" if p.origin == "user" else "deterministic",
                "derived_from": "project hints shared by several sources" if p.origin == "ai" else None,
            },
        }
    )


@router.get("/projects")
async def list_projects(
    user: User,
    factory: Factory,
    codec: Cursors,
    verification: Literal["suggested", "confirmed", "rejected", "user_created"] | None = None,
    cursor: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    scope = f"projects:{verification}"
    async with factory(user_id=user.user_id) as uow:
        items, next_key = await projects.list_projects(
            uow,
            verification=verification,
            after=codec.decode(cursor, user_id=user.user_id, scope=scope),
            limit=limit_of(limit),
        )
    return page_body([project_json(p) for p in items], next_key, codec=codec, user=user, scope=scope)


class CreateProject(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    aliases: list[str] = Field(default_factory=list, max_length=20)


@router.post("/projects", status_code=201)
async def create_project(
    body: CreateProject, request: Request, user: User, factory: Factory, key: IdempotencyKey = None
) -> JSONResponse:
    async def run(uow: UnitOfWork) -> tuple[int, dict[str, Any]]:
        p = await projects.create_project(
            uow,
            name=body.name,
            description=body.description,
            aliases=body.aliases,
            request_key=request_key(key),
            at=now(),
        )
        return 201, project_json(p)

    return await idempotent(factory, user, request, key, body.model_dump(), run)


def _ai_client(request: Request) -> AIClient | None:
    client = getattr(request.app.state, "ai_client", None)
    return client if isinstance(client, AIClient) else None


@router.get("/projects/{project_id}")
async def get_project(project_id: UUID, user: User, factory: Factory) -> JSONResponse:
    """Project context (deterministic): card, members, open items, decisions, recent changes, threads."""
    plan = retrieval.Plan(intent="project", planner="fixed")
    async with factory(user_id=user.user_id) as uow:
        ctx, _, _ = await retrieval.context_for(uow, plan=plan, session=retrieval.SessionState(), now=now())
        page = await retrieval.project_context_for(ctx, project_id)
    sections: dict[str, list[dict[str, Any]]] = {}
    for card in page.cards[1:]:
        sections.setdefault(card.kind, []).append(_card_json(card))
    body = project_json(page.project) | {
        "activity_status": page.status,
        "last_activity_at": page.last_activity.isoformat() if page.last_activity else None,
        "items": sections.get("work_item", []),
        "decisions": sections.get("decision", []),
        "threads": sections.get("conversation", []),
        "meetings": sections.get("meeting", []),
        "supporting_excerpts": sections.get("chunk", []),
        "notes": list(ctx.notes),
    }
    return JSONResponse(as_json(body), headers={"ETag": etag(page.project.version)})


class PatchProject(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    aliases: list[str] | None = Field(default=None, max_length=20)
    importance_user: int | None = Field(default=None, ge=1, le=5)
    status: Literal["active", "archived"] | None = None


@router.patch("/projects/{project_id}")
async def patch_project(
    project_id: UUID, body: PatchProject, user: User, factory: Factory, if_match: IfMatch = None
) -> JSONResponse:
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise ValidationFailed("nothing to change")
    async with factory(user_id=user.user_id) as uow:
        p = await projects.edit_project(
            uow,
            project_id,
            changes,
            if_match=parse_if_match(if_match),
            request_key=request_key(None),
            at=now(),
        )
    return JSONResponse(project_json(p), headers={"ETag": etag(p.version)})


class AssignItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: UUID


@router.post("/projects/{project_id}/items", status_code=204)
async def assign_item(
    project_id: UUID, body: AssignItem, user: User, factory: Factory, key: IdempotencyKey = None
) -> Response:
    async with factory(user_id=user.user_id) as uow:
        await projects.assign_item(uow, project_id, body.item_id, request_key=request_key(key), at=now())
    return Response(status_code=204)


@router.post("/projects/{project_id}/{command}")
async def project_command(
    project_id: UUID,
    command: Literal["confirm", "reject"],
    user: User,
    factory: Factory,
    key: IdempotencyKey = None,
) -> JSONResponse:
    async with factory(user_id=user.user_id) as uow:
        p = await projects.verify_project(
            uow, project_id, confirm=command == "confirm", request_key=request_key(key), at=now()
        )
    return JSONResponse(project_json(p), headers={"ETag": etag(p.version)})


@router.get("/topics")
async def topics(q: str, request: Request, user: User, factory: Factory) -> dict[str, Any]:
    """Topic mode (CONTEXT_ARCHITECTURE.md §10.3): up to 3 groups by thread or meeting."""
    topic = q.strip()
    if not topic or len(topic) > 200:
        raise ValidationFailed("q must be 1-200 characters")
    vector, model = await retrieval.embed_question(_ai_client(request), topic, user_id=user.user_id)
    plan = retrieval.Plan(intent="project", planner="fixed", topic=topic)
    async with factory(user_id=user.user_id) as uow:
        ctx, _, _ = await retrieval.context_for(
            uow,
            plan=plan,
            session=retrieval.SessionState(),
            now=now(),
            query_vector=vector,
            embedding_model=model,
        )
        result = await retrieval.topic_mode(ctx)
    groups: dict[str, list[dict[str, Any]]] = {}
    for card in result.items:
        groups.setdefault(card.group or "group", []).append(_card_json(card))
    return as_json(
        {
            "label": retrieval.TOPIC_LABEL,
            "groups": [{"name": name, "entries": entries} for name, entries in groups.items()],
            "matched": result.matched,
            "full_text_only": vector is None,
        }
    )
