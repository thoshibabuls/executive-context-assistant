"""Chat routes (slice 2.4, BACKEND_DESIGN.md §16.5, §16.7).

``POST /chat/sessions/{id}/messages`` streams Server-Sent Events: ``plan``, ``sources``,
``delta``, ``final`` (or ``error``). The ``Idempotency-Key`` header is required: the turn is
claimed before the stream starts (409 while in progress, 422 when reused with another body), a
completed key replays the stored answer, and a failed run releases the key. Rate limit: 20
messages per minute per user.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from eca import chat
from eca.api.common import Cursors, Factory, IdempotencyKey, User, as_json, limit_of, now, page_body
from eca.api.ratelimit import RateLimiter
from eca.intelligence import AIClient
from eca.platform.errors import ValidationFailed
from eca.platform.idempotency import request_hash

router = APIRouter(prefix="/api/v1/chat")
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def _client(request: Request) -> AIClient | None:
    client = getattr(request.app.state, "ai_client", None)
    return client if isinstance(client, AIClient) else None


def _limiter(request: Request) -> RateLimiter:
    limiter = getattr(request.app.state, "rate_limiter", None)
    if not isinstance(limiter, RateLimiter):
        limiter = RateLimiter()
        request.app.state.rate_limiter = limiter
    return limiter


def session_json(s: chat.SessionView) -> dict[str, Any]:
    return as_json(
        {
            "id": s.id,
            "title": s.title,
            "scope": s.scope,
            "focus": s.focus,
            "version": s.version,
            "created_at": s.created_at,
            "last_active_at": s.last_active_at,
        }
    )


class CreateSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: dict[str, Any] | None = None


@router.post("/sessions", status_code=201)
async def create_session(body: CreateSession, user: User, factory: Factory) -> dict[str, Any]:
    async with factory(user_id=user.user_id) as uow:
        s = await chat.create_session(uow, scope=body.scope, now=now())
    return session_json(s)


@router.get("/sessions")
async def list_sessions(
    user: User, factory: Factory, codec: Cursors, cursor: str | None = None, limit: int | None = None
) -> dict[str, Any]:
    scope = "chat_sessions"
    async with factory(user_id=user.user_id) as uow:
        items, next_key = await chat.list_sessions(
            uow, after=codec.decode(cursor, user_id=user.user_id, scope=scope), limit=limit_of(limit)
        )
    return page_body([session_json(s) for s in items], next_key, codec=codec, user=user, scope=scope)


@router.get("/sessions/{session_id}")
async def get_session(session_id: UUID, user: User, factory: Factory) -> dict[str, Any]:
    async with factory(user_id=user.user_id) as uow:
        s = await chat.get_session(uow, session_id)
        messages = await chat.messages_of(uow, session_id)
    return session_json(s) | {"messages": [chat.message_payload(m) for m in messages]}


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(session_id: UUID, user: User, factory: Factory) -> Response:
    async with factory(user_id=user.user_id) as uow:
        await chat.delete_session(uow, session_id)
    return Response(status_code=204)


class PostMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=2000)
    conversation_id: UUID | None = None  # the open email thread, for "what's this about?" (S1)


def _sse(event: chat.ChatEvent) -> bytes:
    return f"event: {event.name}\ndata: {json.dumps(event.data, default=str)}\n\n".encode()


async def _stream(events: AsyncIterator[chat.ChatEvent]) -> AsyncIterator[bytes]:
    async for event in events:
        yield _sse(event)


async def _replay(events: Iterable[chat.ChatEvent]) -> AsyncIterator[bytes]:
    for event in events:
        yield _sse(event)


@router.post(
    "/sessions/{session_id}/messages",
    response_class=StreamingResponse,
    responses={
        200: {"content": {"text/event-stream": {}}, "description": "plan, sources, delta, final or error"}
    },
)
async def post_message(
    session_id: UUID,
    body: PostMessage,
    request: Request,
    user: User,
    factory: Factory,
    key: IdempotencyKey = None,
) -> StreamingResponse:
    if not key:
        raise ValidationFailed("Idempotency-Key is required for chat messages")
    _limiter(request).check(user.user_id, "chat")
    text = body.text.strip()
    if not text:
        raise ValidationFailed("text must not be blank")
    digest = request_hash(request.method, request.url.path, body.model_dump(mode="json"))
    at = now()
    started = await chat.start_turn(
        factory,
        user_id=user.user_id,
        session_id=session_id,
        text=text,
        conversation_id=body.conversation_id,
        key=key,
        digest=digest,
        now=at,
    )
    if isinstance(started, chat.Replay):
        return StreamingResponse(
            _replay(chat.replay_events(started)),
            media_type="text/event-stream",
            headers=SSE_HEADERS | {"Idempotent-Replay": "true"},
        )
    return StreamingResponse(
        _stream(chat.run_turn(factory, _client(request), started, now=at)),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


class Feedback(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rating: str = Field(pattern=r"^(helpful|not_helpful)$")
    reason: str | None = Field(default=None, max_length=500)


@router.post("/messages/{message_id}/feedback", status_code=204)
async def message_feedback(message_id: UUID, body: Feedback, user: User, factory: Factory) -> Response:
    async with factory(user_id=user.user_id) as uow:
        await chat.record_message_feedback(uow, message_id, rating=body.rating, reason=body.reason)
    return Response(status_code=204)


# --- reply guidance (slice 3.3, BACKEND_DESIGN.md §16.8) ---------------------------------------------

guidance_router = APIRouter(prefix="/api/v1")


class ReplyGuidanceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instructions: str | None = Field(default=None, max_length=500)  # the user's intent for the reply


@guidance_router.post(
    "/conversations/{conversation_id}/reply-guidance",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}, "description": "sources, then final or error"}},
)
async def reply_guidance(
    conversation_id: UUID,
    body: ReplyGuidanceBody,
    request: Request,
    user: User,
    factory: Factory,
    key: IdempotencyKey = None,
) -> StreamingResponse:
    """AI-08 guidance with a copy-only draft: never sent, never written to Gmail (read-only scopes).
    ``Idempotency-Key`` required; 10 requests per minute."""
    if not key:
        raise ValidationFailed("Idempotency-Key is required for reply guidance")
    _limiter(request).check(user.user_id, "reply_guidance")
    instructions = (body.instructions or "").strip() or None
    digest = request_hash(request.method, request.url.path, body.model_dump(mode="json"))
    at = now()
    started = await chat.start_guidance(
        factory,
        user_id=user.user_id,
        conversation_id=conversation_id,
        instructions=instructions,
        key=key,
        digest=digest,
        now=at,
    )
    if isinstance(started, chat.GuidanceReplay):
        return StreamingResponse(
            _replay(chat.guidance_replay_events(started)),
            media_type="text/event-stream",
            headers=SSE_HEADERS | {"Idempotent-Replay": "true"},
        )
    return StreamingResponse(
        _stream(chat.run_guidance(factory, _client(request), started, now=at)),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
