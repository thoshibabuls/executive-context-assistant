"""Shared HTTP helpers for the slice 1.7 routers: cursors, ETags, idempotency, serialization."""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import Depends, Header, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from eca.api.auth import CurrentUser, current_user, uow_factory
from eca.platform.cursors import CursorCodec, clamp_limit
from eca.platform.errors import ValidationFailed
from eca.platform.idempotency import claim_key, request_hash, store_response
from eca.platform.uow import UnitOfWork, UnitOfWorkFactory

User = Annotated[CurrentUser, Depends(current_user)]
Factory = Annotated[UnitOfWorkFactory, Depends(uow_factory)]


def now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def cursors(request: Request) -> CursorCodec:
    codec: CursorCodec = request.app.state.cursors
    return codec


Cursors = Annotated[CursorCodec, Depends(cursors)]


def page_body(
    items: list[Any], next_key: list[Any] | None, *, codec: CursorCodec, user: CurrentUser, scope: str
) -> dict[str, Any]:
    return {
        "items": jsonable_encoder(items),
        "next_cursor": codec.encode(user_id=user.user_id, scope=scope, key=next_key) if next_key else None,
    }


def limit_of(limit: int | None) -> int:
    return clamp_limit(limit)


def etag(version: int) -> str:
    return f'W/"{version}"'


def parse_if_match(value: str | None) -> int | None:
    if value is None:
        return None
    raw = value.strip().removeprefix("W/").strip('"')
    try:
        return int(raw)
    except ValueError as exc:
        raise ValidationFailed("If-Match must be an ETag returned by this API") from exc


IfMatch = Annotated[str | None, Header(alias="If-Match")]
IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key")]


def request_key(key: str | None) -> str:
    """Dedupe key for user events: the client's Idempotency-Key, else one per request."""
    return key or f"req:{uuid.uuid4()}"


async def idempotent(
    factory: UnitOfWorkFactory,
    user: CurrentUser,
    request: Request,
    key: str | None,
    body: Any,
    run: Callable[[UnitOfWork], Awaitable[tuple[int, dict[str, Any]]]],
) -> JSONResponse:
    """Run a creating POST once per Idempotency-Key (§16.3); replay the stored response."""
    async with factory(user_id=user.user_id) as uow:
        if key:
            digest = request_hash(request.method, request.url.path, jsonable_encoder(body))
            stored = await claim_key(uow, key, digest, now=now())
            if stored is not None:
                return JSONResponse(
                    stored.body, status_code=stored.status_code, headers={"Idempotent-Replay": "true"}
                )
        status, response = await run(uow)
        encoded = jsonable_encoder(response)
        if key:
            await store_response(uow, key, status_code=status, body=encoded)
    return JSONResponse(encoded, status_code=status)


def as_json(value: Any) -> dict[str, Any]:
    out = jsonable_encoder(value)
    if not isinstance(out, dict):
        raise TypeError("expected an object")
    return out
