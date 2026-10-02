"""Event types, handlers and their registry (BACKEND_DESIGN.md §7, §21 crash-test harness).

A registry is an explicit object. Production code registers into :data:`default_registry` through
:func:`register_event` and :func:`handles`; the dispatcher and the worker take a registry as an
argument, so tests build their own registry and test-only types never reach the default one.
Registration is explicit (import-time decorators in the owning module), never module scanning.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, TypeVar, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from eca.platform.queues import QUEUE_CONCURRENCY
from eca.platform.uow import UnitOfWork, UnitOfWorkFactory

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


T = TypeVar("T")


@dataclass(frozen=True)
class Resources:
    """Process-level dependencies handed to handlers by the worker composition (§5.6).

    A typed lookup keyed by class: ``resources.get(AIClient)``. Domain modules never build these
    themselves, so tests and the worker decide what a handler talks to.
    """

    _values: dict[type, object] = field(default_factory=dict)

    @classmethod
    def of(cls, *values: object) -> Resources:
        return cls({type(v): v for v in values})

    def get(self, kind: type[T]) -> T:
        for key, value in self._values.items():
            if issubclass(key, kind):
                return cast(T, value)
        raise LookupError(f"No resource of type {kind.__name__} was provided to this worker")


HandlerMode = Literal["consumption", "natural_key"]


@dataclass(frozen=True)
class HandlerContext:
    """What a handler receives.

    ``consumption`` handlers get ``uow``: the wrapper's transaction, which already holds the
    ``event_consumptions`` row. ``natural_key`` handlers (external calls, §7.4) get ``uow`` = None
    and run their own short transactions from ``uow_factory``.
    """

    uow: UnitOfWork | None
    envelope: EventEnvelope
    payload: BaseModel  # validated with the payload model registered for the event type
    attempt: int  # earlier runs of this job (0 on the first run)
    uow_factory: UnitOfWorkFactory | None = None
    resources: Resources = field(default_factory=Resources)

    @property
    def tx(self) -> UnitOfWork:
        """The handler transaction of a ``consumption`` handler."""
        if self.uow is None:
            raise RuntimeError("natural_key handlers have no wrapper transaction; use uow_factory")
        return self.uow

    @property
    def factory(self) -> UnitOfWorkFactory:
        if self.uow_factory is None:
            raise RuntimeError("no unit-of-work factory in this handler context")
        return self.uow_factory


HandlerFunc = Callable[[HandlerContext], Awaitable[None]]


@dataclass(frozen=True)
class HandlerSpec:
    name: str  # stable identity: event_consumptions.handler and job lock keys
    event_type: str
    queue: str
    func: HandlerFunc
    mode: HandlerMode = "consumption"
    # Account deletion (§13.3): jobs for a user who is ``deleting`` (or gone) are no-ops, except
    # the deletion job itself.
    runs_while_deleting: bool = False


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
        self,
        event_type: str,
        *,
        name: str,
        queue: str = "events",
        mode: HandlerMode = "consumption",
        runs_while_deleting: bool = False,
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
            self._handlers[name] = HandlerSpec(
                name=name,
                event_type=event_type,
                queue=queue,
                func=func,
                mode=mode,
                runs_while_deleting=runs_while_deleting,
            )
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
