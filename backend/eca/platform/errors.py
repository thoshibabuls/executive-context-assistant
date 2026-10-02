"""Domain errors raised by services and workers (BACKEND_DESIGN.md §14.1).

These carry no HTTP concepts. Only ``eca.api`` translates them into HTTP responses.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FieldError:
    field: str
    message: str


class DomainError(Exception):
    """Base class for expected, typed failures."""

    code: str = "domain_error"

    def __init__(self, message: str = "", *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        self.details: dict[str, Any] = dict(details or {})


class NotFound(DomainError):
    """Unknown resource, or a resource owned by another user (never reveal which)."""

    code = "not_found"


class Conflict(DomainError):
    code = "conflict"

    def __init__(
        self,
        message: str = "",
        *,
        fields: Sequence[str] = (),
        current_version: int | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.fields = list(fields)
        self.current_version = current_version


class PreconditionFailed(DomainError):
    """``reason='version'`` maps to 412; ``reason='scope_required'`` maps to 409."""

    code = "precondition_failed"

    def __init__(
        self, message: str = "", *, reason: str = "version", details: Mapping[str, Any] | None = None
    ) -> None:
        super().__init__(message, details=details)
        self.reason = reason


class ValidationFailed(DomainError):
    code = "validation_failed"

    def __init__(
        self,
        message: str = "",
        *,
        errors: Sequence[FieldError] = (),
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.errors = list(errors)


class PermissionDenied(DomainError):
    code = "permission_denied"


class RateLimited(DomainError):
    code = "rate_limited"

    def __init__(
        self, message: str = "", *, retry_after_s: int | None = None, details: Mapping[str, Any] | None = None
    ) -> None:
        super().__init__(message, details=details)
        self.retry_after_s = retry_after_s


class BudgetExceeded(RateLimited):
    code = "budget_exceeded"


class UpstreamUnavailable(DomainError):
    code = "upstream_unavailable"


class AuthRevoked(DomainError):
    code = "reauth_required"


class CursorExpired(DomainError):
    """Provider sync cursor expired; handled internally by sync jobs, never surfaced over HTTP."""

    code = "cursor_expired"


class Gone(DomainError):
    """The user is being deleted; all work for the user is refused."""

    code = "gone"
