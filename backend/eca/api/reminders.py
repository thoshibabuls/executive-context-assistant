"""Reminders, the notification center and Web Push subscriptions (slice 3.1, BACKEND_DESIGN.md
§16.5, §16.8). Deterministic read models and user state transitions from ``eca.attention``.

Reminder text is rendered from templates over the user's current items; each reminder carries the
underlying item's provenance so AI-derived items stay labelled.
"""

from __future__ import annotations

import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from eca import attention
from eca.api.common import Cursors, Factory, User, as_json, limit_of, now, page_body
from eca.platform.config import get_settings
from eca.platform.errors import NotFound

router = APIRouter(prefix="/api/v1")


def reminder_json(r: attention.ReminderView) -> dict[str, Any]:
    return as_json(
        {
            "id": r.id,
            "item_type": r.item_type,
            "item_id": r.item_id,
            "reminder_type": r.reminder_type,
            "state": r.state,
            "text": r.text,
            "fire_at": r.fire_at,
            "delivered_at": r.delivered_at,
            "snoozed_until": r.snoozed_until,
            "proactive": r.proactive,
            "priority": r.priority,
            "reason": r.reason,
            "origin": "computed",  # reminders are deterministic rules (TECHNICAL_DESIGN.md §15)
            "item_provenance": r.provenance,
            "version": r.version,
        }
    )


@router.get("/reminders")
async def list_reminders(
    user: User,
    factory: Factory,
    codec: Cursors,
    state: Literal["active", "pending", "all"] = "active",
    cursor: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    scope = f"reminders:{state}"
    async with factory(user_id=user.user_id) as uow:
        items, next_key = await attention.list_reminders(
            uow,
            state=state,
            after=codec.decode(cursor, user_id=user.user_id, scope=scope),
            limit=limit_of(limit),
            now=now(),
        )
    return page_body([reminder_json(r) for r in items], next_key, codec=codec, user=user, scope=scope)


class SnoozeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preset: Literal["1h", "3h", "tomorrow"] | None = None
    until: datetime.datetime | None = None


@router.post("/reminders/{reminder_id}/snooze")
async def snooze_reminder(
    reminder_id: UUID, body: SnoozeBody, user: User, factory: Factory
) -> dict[str, Any]:
    async with factory(user_id=user.user_id) as uow:
        r = await attention.snooze(uow, reminder_id, preset=body.preset, until=body.until, now=now())
    return reminder_json(r)


@router.post("/reminders/{reminder_id}/{command}")
async def reminder_command(
    reminder_id: UUID, command: Literal["dismiss", "acted"], user: User, factory: Factory
) -> dict[str, Any]:
    async with factory(user_id=user.user_id) as uow:
        if command == "dismiss":
            r = await attention.dismiss(uow, reminder_id, now=now())
        else:
            r = await attention.mark_acted(uow, reminder_id, now=now())
    return reminder_json(r)


@router.get("/notifications")
async def list_notifications(
    user: User,
    factory: Factory,
    codec: Cursors,
    unread: bool = False,
    cursor: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    scope = f"notifications:{unread}"
    async with factory(user_id=user.user_id) as uow:
        items, next_key = await attention.list_notifications(
            uow,
            unread=unread,
            after=codec.decode(cursor, user_id=user.user_id, scope=scope),
            limit=limit_of(limit),
            now=now(),
        )
    rows = [
        {"id": n.id, "created_at": n.created_at, "read_at": n.read_at, "reminder": reminder_json(n.reminder)}
        for n in items
    ]
    return page_body(rows, next_key, codec=codec, user=user, scope=scope)


class ReadBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[UUID] | None = Field(default=None, max_length=200)
    before: datetime.datetime | None = None


@router.post("/notifications/read")
async def read_notifications(body: ReadBody, user: User, factory: Factory) -> dict[str, Any]:
    async with factory(user_id=user.user_id) as uow:
        count = await attention.mark_read(uow, ids=body.ids, before=body.before, now=now())
    return {"marked": count}


@router.get("/push-subscriptions/key")
async def push_key() -> dict[str, Any]:
    """The VAPID public key for ``PushManager.subscribe``; 404 when Web Push is not configured."""
    keys = attention.VapidKeys.from_settings(get_settings())
    if keys is None:
        raise NotFound("push_not_configured")
    return {"public_key": keys.public_key}


class PushSubscriptionBody(BaseModel):
    """The browser's ``PushSubscription`` JSON; the encryption ``keys`` are accepted and ignored
    (pushes carry no payload, TECHNICAL_DESIGN.md §15.4)."""

    model_config = ConfigDict(extra="ignore")
    endpoint: str = Field(min_length=10, max_length=1000)
    expirationTime: float | None = None  # noqa: N815 (browser field name)


@router.post("/push-subscriptions", status_code=201)
async def create_push_subscription(
    body: PushSubscriptionBody, request: Request, user: User, factory: Factory
) -> dict[str, Any]:
    if attention.VapidKeys.from_settings(get_settings()) is None:
        raise NotFound("push_not_configured")
    expires = (
        datetime.datetime.fromtimestamp(body.expirationTime / 1000, datetime.UTC)
        if body.expirationTime
        else None
    )
    async with factory(user_id=user.user_id) as uow:
        s = await attention.subscribe(
            uow, endpoint=body.endpoint, expires_at=expires, user_agent=request.headers.get("user-agent")
        )
    return as_json({"id": s.id, "endpoint_host": s.endpoint_host, "expires_at": s.expires_at})


@router.delete("/push-subscriptions/{subscription_id}", status_code=204)
async def delete_push_subscription(subscription_id: UUID, user: User, factory: Factory) -> Response:
    async with factory(user_id=user.user_id) as uow:
        await attention.unsubscribe(uow, subscription_id, now=now())
    return Response(status_code=204)
