"""AI-09 ``transcribe`` output schema, version 1 (AI_PIPELINE.md §5.10).

Diarized segments of one audio file (or one 60-minute window of it). ``complete`` is false when
the model could not finish within its output limit; the caller then falls back to windows. Code
validates timestamps against the probed duration and drops empty segments.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "transcribe.v1"


class SegmentOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    speaker: str = Field(min_length=1, max_length=40)
    text: str = Field(max_length=2000)


class Transcription(BaseModel):
    model_config = ConfigDict(extra="forbid")

    segments: list[SegmentOut] = Field(default_factory=list, max_length=4000)
    complete: bool = True
