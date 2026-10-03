"""Events published by ``meetings``."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from eca.platform.events import register_event

MEETING_CHANGED = "MeetingChanged"
RECORDING_UPLOADED = "RecordingUploaded"
RECORDING_STAGE_DUE = "RecordingStageDue"


class MeetingChanged(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    meeting_id: UUID
    status: str
    version: int


class RecordingUploaded(BaseModel):
    """Upload complete (BACKEND_DESIGN.md §11.4): the media pipeline starts with ``media_prepare``."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    recording_id: UUID


class RecordingStageDue(BaseModel):
    """Run a recording's stage again: the retry endpoint or ``media_sweep`` (backoff, budget)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    recording_id: UUID
    stage: str  # prepare | transcribe | extract


register_event(MEETING_CHANGED, MeetingChanged)
register_event(RECORDING_UPLOADED, RecordingUploaded)
register_event(RECORDING_STAGE_DUE, RecordingStageDue)
