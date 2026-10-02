"""Provenance envelope on every AI-derived object (AI_PIPELINE.md §5.1, §5.3).

An object without a source and, for LLM methods, without at least one evidence span is not
created. The confidence band must match the confidence value.
"""

from __future__ import annotations

import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ExtractionMethod = Literal[
    "llm", "llm_adjudicated", "rule", "deterministic", "embedding_match", "transcription"
]
ConfidenceBand = Literal["low", "medium", "high"]
SourceKind = Literal["message", "meeting", "recording", "calendar_event"]
LLM_METHODS = frozenset({"llm", "llm_adjudicated", "transcription"})


def confidence_band(confidence: float) -> ConfidenceBand:
    """Bands: low < 0.6 ≤ medium < 0.8 ≤ high."""
    if confidence < 0.6:
        return "low"
    if confidence < 0.8:
        return "medium"
    return "high"


class SourceRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_item_id: UUID
    kind: SourceKind
    occurred_at: datetime.datetime


class EvidenceSpan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: UUID
    quote: str = Field(min_length=1)
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=0)
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _ranges(self) -> EvidenceSpan:
        if (self.char_start is None) != (self.char_end is None):
            raise ValueError("char_start and char_end go together")
        if self.char_start is not None and self.char_end is not None and self.char_end <= self.char_start:
            raise ValueError("char_end must be after char_start")
        if self.start_ms is not None and self.end_ms is not None and self.end_ms < self.start_ms:
            raise ValueError("end_ms must not be before start_ms")
        return self


class Provenance(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source: tuple[SourceRef, ...] = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    confidence_band: ConfidenceBand
    derived_at: datetime.datetime
    extraction_method: ExtractionMethod
    model: str | None = None
    prompt_version: str | None = None
    extraction_id: UUID | None = None
    evidence: tuple[EvidenceSpan, ...] = ()

    @field_validator("derived_at")
    @classmethod
    def _aware(cls, v: datetime.datetime) -> datetime.datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("derived_at must be timezone-aware")
        return v

    @model_validator(mode="after")
    def _method_rules(self) -> Provenance:
        if self.confidence_band != confidence_band(self.confidence):
            raise ValueError("confidence_band does not match confidence")
        if self.extraction_method in LLM_METHODS:
            if not self.model:
                raise ValueError("model is required for model-derived objects")
            if self.extraction_method != "transcription" and not self.prompt_version:
                raise ValueError("prompt_version is required for LLM methods")
            if not self.evidence:
                raise ValueError("at least one evidence span is required for model-derived objects")
        elif self.model is not None or self.prompt_version is not None:
            raise ValueError("model and prompt_version are only set for model-derived objects")
        return self
