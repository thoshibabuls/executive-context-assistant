"""Events published by ``meetings``."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from eca.platform.events import register_event

MEETING_CHANGED = "MeetingChanged"
RECORDING_UPLOADED = "RecordingUploaded"
RECORDING_STAGE_DUE = "RecordingStageDue"
RECORDING_PREPARED = "RecordingPrepared"
TRANSCRIPT_STORED = "TranscriptStored"
SPEAKER_MAPPING_CHANGED = "SpeakerMappingChanged"
MEETING_PROCESSED = "MeetingProcessed"
MEETING_ASKS_REQUESTED = "MeetingAsksRequested"


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


class RecordingPrepared(BaseModel):
    """The prepared mono Opus audio is stored: ``transcribe`` (AI-09) runs next."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    recording_id: UUID


class TranscriptStored(BaseModel):
    """A transcript version is stored (AI-09 or a parsed file): index it and extract the meeting."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    recording_id: UUID
    source_item_id: UUID
    transcription_model: str


class SpeakerMappingChanged(BaseModel):
    """Applied speaker mappings of a meeting changed (user confirmation or AI-10 apply): transcript
    chunks are re-indexed with the new names."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    meeting_id: UUID
    recording_id: UUID | None
    labels: list[str]
    version: int


class MeetingProcessed(BaseModel):
    """AI-10 apply finished for a meeting's recording: prep sections are recomputed."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    meeting_id: UUID
    recording_id: UUID


class MeetingAsksRequested(BaseModel):
    """The user opened the prep view with non-empty sections: AI-11 runs once for this version."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    meeting_id: UUID
    version: int


register_event(MEETING_CHANGED, MeetingChanged)
register_event(RECORDING_UPLOADED, RecordingUploaded)
register_event(RECORDING_STAGE_DUE, RecordingStageDue)
register_event(RECORDING_PREPARED, RecordingPrepared)
register_event(TRANSCRIPT_STORED, TranscriptStored)
register_event(SPEAKER_MAPPING_CHANGED, SpeakerMappingChanged)
register_event(MEETING_PROCESSED, MeetingProcessed)
register_event(MEETING_ASKS_REQUESTED, MeetingAsksRequested)
