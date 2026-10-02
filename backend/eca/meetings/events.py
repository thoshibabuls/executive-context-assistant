"""Events published by ``meetings``."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from eca.platform.events import register_event

MEETING_CHANGED = "MeetingChanged"


class MeetingChanged(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    meeting_id: UUID
    status: str
    version: int


register_event(MEETING_CHANGED, MeetingChanged)
