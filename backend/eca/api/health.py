"""Liveness and readiness endpoints (BACKEND_DESIGN.md §16.5, §19)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from eca.platform.health import check_readiness

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Process is alive. Never touches dependencies."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    """Database reachable and schema at the migration head.

    The worker adds a dispatcher-heartbeat check in slice 0.3.
    """
    report = await check_readiness(request.app.state.engine, head=request.app.state.migration_head)
    body: dict[str, Any] = {
        "status": "ready" if report.ready else "not_ready",
        "checks": {
            "database": report.database,
            "schema_at_head": report.schema_at_head,
        },
        "revision": {"current": report.current_revision, "head": report.head_revision},
    }
    return JSONResponse(body, status_code=200 if report.ready else 503)
