"""Event types, handlers and their registry (BACKEND_DESIGN.md §7, §21 crash-test harness).

A registry is an explicit object. Production code registers into :data:`default_registry` through
:func:`register_event` and :func:`handles`; the dispatcher and the worker take a registry as an
argument, so tests build their own registry and test-only types never reach the default one.
Registration is explicit (import-time decorators in the owning module), never module scanning.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from eca.platform.queues import QUEUE_CONCURRENCY
from eca.platform.uow import UnitOfWork

_EVENT_TYPE = re.compile(r"^[A-Za-z][A-Za-z0-9_.]{2,99}$")
_HANDLER_NAME = re.compile(r"^[a-z][a-z0-9_.]{2,99}$")


class UnregisteredEventType(LookupError):
    """An event type that no ``register_event`` call declared."""


class EventEnvelope(BaseModel):
    """What a handler job receives. Carries IDs and small facts only, never content."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    event_type: str
    user_id: UUID | None
    aggregate_type: str
    aggregate_id: UUID
    payload: dict[str, Any]
    correlation: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True)
class NewEvent:
    """An event to publish. ``user_id`` is never part of it: it comes from the unit of work."""

    event_type: str
    aggregate_type: str
    aggregate_id: UUID
    payload: BaseModel


@dataclass(frozen=True)
class HandlerContext:
    uow: UnitOfWork  # the handler's own transaction (worker role, app.user_id = event user)
    envelope: EventEnvelope
    payload: BaseModel  # validated with the payload model registered for the event type
    attempt: int  # earlier runs of this job (0 on the first run)


HandlerFunc = Callable[[HandlerContext], Awaitable[None]]


@dataclass(frozen=True)
class HandlerSpec:
    name: str  # stable identity: event_consumptions.handler and job lock keys
    event_type: str
    queue: str
    func: HandlerFunc


class EventRegistry:
    def __init__(self) -> None:
        self._payload_models: dict[str, type[BaseModel]] = {}
        self._handlers: dict[str, HandlerSpec] = {}

    def register_event(self, event_type: str, payload_model: type[BaseModel]) -> None:
        if not _EVENT_TYPE.fullmatch(event_type):
            raise ValueError(f"Invalid event type name: {event_type!r}")
        if not (isinstance(payload_model, type) and issubclass(payload_model, BaseModel)):
            raise TypeError("payload_model must be a pydantic model class")
        if event_type in self._payload_models:
            raise ValueError(f"Event type already registered: {event_type!r}")
        self._payload_models[event_type] = payload_model

    def handles(
        self, event_type: str, *, name: str, queue: str = "events"
    ) -> Callable[[HandlerFunc], HandlerFunc]:
        if not _HANDLER_NAME.fullmatch(name):
            raise ValueError(f"Invalid handler name: {name!r}")
        if event_type not in self._payload_models:
            raise UnregisteredEventType(f"Register event type {event_type!r} before its handlers")
        if queue not in QUEUE_CONCURRENCY:
            raise ValueError(f"Unknown queue: {queue!r}")

        def decorator(func: HandlerFunc) -> HandlerFunc:
            if name in self._handlers:
                raise ValueError(f"Handler name already registered: {name!r}")
            self._handlers[name] = HandlerSpec(name=name, event_type=event_type, queue=queue, func=func)
            return func

        return decorator

    def is_registered(self, event_type: str) -> bool:
        return event_type in self._payload_models

    def payload_model(self, event_type: str) -> type[BaseModel]:
        try:
            return self._payload_models[event_type]
        except KeyError:
            raise UnregisteredEventType(event_type) from None

    def handlers_for(self, event_type: str) -> list[HandlerSpec]:
        """Handlers of a registered event type, in name order (deterministic dispatch)."""
        self.payload_model(event_type)
        return sorted(
            (h for h in self._handlers.values() if h.event_type == event_type), key=lambda h: h.name
        )

    def handler(self, name: str) -> HandlerSpec:
        return self._handlers[name]

    def handlers(self) -> list[HandlerSpec]:
        return sorted(self._handlers.values(), key=lambda h: h.name)


default_registry = EventRegistry()
register_event = default_registry.register_event
handles = default_registry.handles
