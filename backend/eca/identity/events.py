"""Events published by ``identity``."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from eca.platform.events import register_event

USER_CREATED = "UserCreated"
USER_DELETION_REQUESTED = "UserDeletionRequested"


class UserCreated(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    user_id: UUID


class UserDeletionRequested(BaseModel):
    """Recorded by ``DELETE /api/v1/me``; the deletion job itself is slice 1.9."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    user_id: UUID
    deletion_job_id: UUID


register_event(USER_CREATED, UserCreated)
register_event(USER_DELETION_REQUESTED, UserDeletionRequested)
