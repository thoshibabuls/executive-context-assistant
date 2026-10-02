"""Attempt caps and retry decisions (AI_PIPELINE.md §7, BACKEND_DESIGN.md §5.5).

Background work: at most 4 model calls per extraction key including one repair; from attempt
3 the role's fallback model; budget deferrals do not count. The persistent per-key counter is
stored with ``extractions`` (slice 1.4); this module decides what the next call is.

Interactive work: one retry, then one fallback call, then degradation (``AI_PIPELINE.md`` §14).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeVar

from eca.intelligence.provider.types import (
    AIError,
    EmptyResponse,
    ProviderError,
    SafetyBlocked,
    SchemaInvalid,
)

BACKGROUND_ATTEMPT_CAP = 4
FALLBACK_FROM_ATTEMPT = 3

T = TypeVar("T")


class ErrorKind(StrEnum):
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    PROVIDER_ERROR = "provider_error"
    SCHEMA_INVALID = "schema_invalid"
    SAFETY_BLOCK = "safety_block"
    EMPTY_RESPONSE = "empty_response"
    BUDGET_EXCEEDED = "budget_exceeded"


class Action(StrEnum):
    RETRY = "retry"
    REPAIR = "repair"
    FAIL_PERMANENT = "failed_permanent"
    DEFER = "defer"


@dataclass(frozen=True)
class Decision:
    action: Action
    use_fallback: bool = False
    counts_toward_cap: bool = True


_RETRYABLE = {ErrorKind.TIMEOUT, ErrorKind.RATE_LIMITED, ErrorKind.PROVIDER_ERROR}


def classify(error: BaseException) -> ErrorKind:
    """Map an AI-layer error to the attempt-policy kind."""
    if isinstance(error, SchemaInvalid):
        return ErrorKind.SCHEMA_INVALID
    if isinstance(error, SafetyBlocked):
        return ErrorKind.SAFETY_BLOCK
    if isinstance(error, EmptyResponse):
        return ErrorKind.EMPTY_RESPONSE
    if isinstance(error, ProviderError):
        return ErrorKind(error.status.value)
    raise TypeError(f"not an AI provider error: {type(error).__name__}")


def next_background_attempt(
    kind: ErrorKind, *, calls_made: int, repairs_made: int, cap: int = BACKGROUND_ATTEMPT_CAP
) -> Decision:
    """What to do after a failed call, given the calls already made for this key (≥ 1)."""
    if calls_made < 1:
        raise ValueError("calls_made counts the failed call and must be >= 1")
    if kind is ErrorKind.BUDGET_EXCEEDED:
        return Decision(Action.DEFER, counts_toward_cap=False)
    if kind in (ErrorKind.SAFETY_BLOCK, ErrorKind.EMPTY_RESPONSE):
        return Decision(Action.FAIL_PERMANENT)
    if calls_made >= cap:
        return Decision(Action.FAIL_PERMANENT)
    use_fallback = calls_made + 1 >= FALLBACK_FROM_ATTEMPT
    if kind is ErrorKind.SCHEMA_INVALID:
        if repairs_made >= 1:
            return Decision(Action.FAIL_PERMANENT)
        return Decision(Action.REPAIR, use_fallback=use_fallback)
    if kind in _RETRYABLE:
        return Decision(Action.RETRY, use_fallback=use_fallback)
    raise ValueError(f"unhandled error kind {kind}")


class Degraded(AIError):
    """An interactive call gave up; the caller renders the degraded route (AI_PIPELINE.md §14)."""

    def __init__(self, kind: ErrorKind) -> None:
        super().__init__(f"interactive AI call degraded after {kind.value}")
        self.kind = kind


async def run_interactive(call: Callable[[bool], Awaitable[T]]) -> T:
    """Interactive policy: primary, one retry on the primary, one call on the fallback, then degrade.

    ``call(use_fallback)`` performs one provider call. Safety blocks and empty responses degrade
    immediately (retrying does not change them).
    """
    plan = (False, False, True)
    last: ErrorKind | None = None
    for use_fallback in plan:
        try:
            return await call(use_fallback)
        except (ProviderError, SchemaInvalid) as exc:
            last = classify(exc)
            if last in (ErrorKind.SAFETY_BLOCK, ErrorKind.EMPTY_RESPONSE):
                raise Degraded(last) from exc
    assert last is not None
    raise Degraded(last)
