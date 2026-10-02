"""Translation of domain errors into RFC 9457 Problem Details (BACKEND_DESIGN.md §14.2).

This is the only place that maps service/worker errors to HTTP. Services never raise
``HTTPException``.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from eca.platform import errors

PROBLEM_CONTENT_TYPE = "application/problem+json"
TYPE_BASE = "https://errors.eca.app/"

log = structlog.get_logger("eca.api.problems")


def status_for(error: errors.DomainError) -> tuple[int, str]:
    """Return (HTTP status, problem code) for a domain error."""
    if isinstance(error, errors.ValidationFailed):
        return 422, error.code
    if isinstance(error, errors.NotFound):
        return 404, error.code
    if isinstance(error, errors.Conflict):
        return 409, error.code
    if isinstance(error, errors.PreconditionFailed):
        return (412, error.code) if error.reason == "version" else (409, "scope_required")
    if isinstance(error, errors.PermissionDenied):
        return 403, error.code
    if isinstance(error, errors.RateLimited):  # includes BudgetExceeded
        return 429, error.code
    if isinstance(error, errors.UpstreamUnavailable):
        return 503, error.code
    if isinstance(error, errors.AuthRevoked):
        return 409, error.code
    if isinstance(error, errors.Gone):
        return 410, error.code
    return 500, "internal_error"  # CursorExpired and unknown subclasses must never reach HTTP


def _request_id(request: Request) -> str | None:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) else None


def problem_response(
    request: Request,
    *,
    status: int,
    code: str,
    title: str,
    detail: str | None = None,
    extra: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": TYPE_BASE + code,
        "title": title,
        "status": status,
        "instance": request.url.path,
        "code": code,
        "request_id": _request_id(request),
    }
    if detail:
        body["detail"] = detail
    if extra:
        body.update(extra)
    return JSONResponse(body, status_code=status, media_type=PROBLEM_CONTENT_TYPE, headers=headers)


async def _domain_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, errors.DomainError)
    status, code = status_for(exc)
    if status >= 500:
        log.error("unmapped_domain_error", error_code=exc.code)
        return problem_response(request, status=500, code="internal_error", title="Internal error")
    extra: dict[str, Any] = {}
    headers: dict[str, str] | None = None
    if isinstance(exc, errors.ValidationFailed):
        extra["errors"] = [{"field": e.field, "message": e.message} for e in exc.errors]
    if isinstance(exc, errors.Conflict):
        extra["errors"] = [{"field": f, "message": "changed by another update"} for f in exc.fields]
        if exc.current_version is not None:
            extra["current_version"] = exc.current_version
    if isinstance(exc, errors.RateLimited) and exc.retry_after_s is not None:
        headers = {"Retry-After": str(exc.retry_after_s)}
    return problem_response(
        request,
        status=status,
        code=code,
        title=code.replace("_", " ").capitalize(),
        detail=exc.message,
        extra=extra,
        headers=headers,
    )


async def _validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    field_errors = [
        {
            "field": ".".join(str(p) for p in err.get("loc", ()) if p not in ("body", "query", "path")),
            "message": err.get("msg", "invalid"),
        }
        for err in exc.errors()
    ]
    return problem_response(
        request,
        status=422,
        code="validation_failed",
        title="Validation failed",
        extra={"errors": field_errors},
    )


async def _http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, f"http_{exc.status_code}")
    return problem_response(request, status=exc.status_code, code=code, title=str(exc.detail))


async def _unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled_error", error_type=type(exc).__name__)
    return problem_response(request, status=500, code="internal_error", title="Internal error")


def install_problem_handlers(app: FastAPI) -> None:
    app.add_exception_handler(errors.DomainError, _domain_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(Exception, _unhandled_error_handler)
