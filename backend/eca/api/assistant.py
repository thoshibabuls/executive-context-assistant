"""Change feed, checkpoints and day view routes (slice 2.3, BACKEND_DESIGN.md §16.5, §16.7).

Deterministic read models from ``eca.retrieval``; checkpoints through ``eca.identity``. Every
entry keeps its provenance label (data class, claim kind) so AI-derived facts stay labelled.
"""

from __future__ import annotations

import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Path, Response
from pydantic import BaseModel, ConfigDict

from eca import identity, retrieval
from eca.api.common import Cursors, Factory, User, as_json, limit_of, now, page_body
from eca.platform.errors import ValidationFailed

router = APIRouter(prefix="/api/v1")


def _card_json(card: retrieval.PacketItem) -> dict[str, Any]:
    return {
        "kind": card.kind,
        "entity_id": card.entity_id,
        "line": card.line,
        "data_class": card.data_class,
        "claim_kind": card.claim_kind,
        "user_backed": card.user_backed,
        "source_item_ids": list(card.source_item_ids),
        "group": card.group,
    }


def _window_json(w: retrieval.TimeWindow) -> dict[str, Any]:
    return {"start": w.start, "end": w.end, "label": w.label, "basis": w.basis, "note": w.note}


def _coverage_json(c: retrieval.Coverage) -> dict[str, Any]:
    return {"text": c.text, **c.summary()}


@router.get("/changes")
async def get_changes(
    user: User,
    factory: Factory,
    codec: Cursors,
    since: datetime.datetime | None = None,
    scope: str = "today",
    cursor: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    if since is not None and since.tzinfo is None:
        raise ValidationFailed("since must include a timezone offset")
    key_scope = f"changes:{scope}:{since.isoformat() if since else ''}"
    async with factory(user_id=user.user_id) as uow:
        page = await retrieval.change_feed(
            uow,
            scope=scope,
            since=since,
            now=now(),
            after=codec.decode(cursor, user_id=user.user_id, scope=key_scope),
            limit=limit_of(limit),
        )
    items = [
        {
            "group": e.change.group,
            "entity_type": e.change.entity_type,
            "entity_id": e.change.entity_id,
            "materiality": e.change.materiality,
            "recorded_at": e.change.recorded_at,
            "before": e.change.before,
            "after": e.change.after,
            "detail": list(e.change.detail),
            **_card_json(e.card),
        }
        for e in page.entries
    ]
    body = page_body(items, page.next_key, codec=codec, user=user, scope=key_scope)
    return as_json(body | {"anchor": _window_json(page.anchor), "coverage": _coverage_json(page.coverage)})


class CheckpointBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    last_seen_at: datetime.datetime | None = None


@router.put("/checkpoints/{surface}", status_code=204)
async def put_checkpoint(
    surface: str, user: User, factory: Factory, body: CheckpointBody | None = None
) -> Response:
    at = body.last_seen_at if body and body.last_seen_at else now()
    if at.tzinfo is None:
        raise ValidationFailed("last_seen_at must include a timezone offset")
    async with factory(user_id=user.user_id) as uow:
        await identity.mark_seen(uow, surface, at=min(at, now()))
    return Response(status_code=204)


@router.get("/days/{date}")
async def get_day(
    date: Annotated[datetime.date, Path(description="Local calendar date (YYYY-MM-DD) in your timezone")],
    user: User,
    factory: Factory,
) -> dict[str, Any]:
    async with factory(user_id=user.user_id) as uow:
        page = await retrieval.day_view_for(uow, day=date, now=now())
    sections: dict[str, list[dict[str, Any]]] = {}
    for card in page.cards:
        sections.setdefault(card.group or card.kind, []).append(_card_json(card))
    return as_json(
        {
            "date": date,
            "window": _window_json(page.window),
            "coverage": _coverage_json(page.coverage),
            "meetings": sections.pop("meeting", []),
            "decisions": sections.pop("decisions", []),
            "items": sections.pop("items", []),
            "threads": sections.pop("threads", []),
            "awaiting_reply": sections.pop("now awaiting your reply", []),
            "learned_today_events": len(page.view.learned_today),
        }
    )
