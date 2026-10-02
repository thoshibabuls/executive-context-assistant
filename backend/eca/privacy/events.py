"""Events published by ``privacy``."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from eca.platform.events import register_event

SOURCE_PURGE_REQUESTED = "SourcePurgeRequested"


class SourcePurgeRequested(BaseModel):
    """``DELETE /api/v1/connections/{id}?purge=true``: purge the connection's source data."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    deletion_job_id: UUID
    connection_id: UUID


register_event(SOURCE_PURGE_REQUESTED, SourcePurgeRequested)
