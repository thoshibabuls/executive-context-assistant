"""AI-10 ``meeting_extract`` output schema, version 1 (AI_PIPELINE.md §5.6, §5.10).

The §5.2 statement shapes with a transcript speaker label instead of the email sender, and
evidence that points at a transcript segment. Code grounds every quote in its segment, maps
statements to types and directions (§5.5), resolves dates (§5.4) and decides speaker mappings
(auto-apply only at confidence 0.9 or more). ``due_iso_guess`` is diagnostic only.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from eca.intelligence.output_schemas.email_extract import Mention, Signal, StatementKind

SCHEMA_VERSION = "meeting_extract.v1"

_REF = r"^(speaker|self|unknown|speaker:.+|name:.+|email:.+)$"
_CANDIDATE = r"^C([1-9]|1[0-2])$"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SegmentEvidence(_Model):
    segment_seq: int = Field(ge=0)
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)
    quote: str = Field(min_length=1, max_length=600)


class MeetingStatement(_Model):
    statement_kind: StatementKind
    action: str = Field(min_length=1, max_length=120)
    speaker: str = Field(min_length=1, max_length=60)  # a transcript speaker label
    owner_ref: str = Field(pattern=_REF)
    beneficiary_ref: str | None = Field(default=None, pattern=_REF)
    due_text: str | None = Field(default=None, max_length=80)
    due_iso_guess: str | None = None
    candidate_id: str | None = Field(default=None, pattern=_CANDIDATE)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: SegmentEvidence


class MeetingSignal(_Model):
    candidate_id: str = Field(pattern=_CANDIDATE)
    signal: Signal
    speaker: str = Field(min_length=1, max_length=60)
    new_due_text: str | None = Field(default=None, max_length=80)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: SegmentEvidence


class MeetingDecision(_Model):
    kind: Literal["decision", "open_question"]
    statement: str = Field(min_length=1, max_length=300)
    candidate_id: str | None = Field(default=None, pattern=_CANDIDATE)
    relation: Literal["none", "resolves", "supersedes", "contradicts"] = "none"
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: SegmentEvidence


class Concern(_Model):
    text: str = Field(min_length=1, max_length=300)
    evidence: SegmentEvidence


class SpeakerProposal(_Model):
    label: str = Field(min_length=1, max_length=60)
    person_email: str | None = Field(default=None, max_length=320)
    person_name: str | None = Field(default=None, max_length=120)
    confidence: float = Field(ge=0.0, le=1.0)
    quote: str = Field(default="", max_length=300)


class MeetingExtraction(_Model):
    summary: str = Field(default="", max_length=1200)  # at most about 150 words
    topics: list[str] = Field(default_factory=list, max_length=10)
    concerns: list[Concern] = Field(default_factory=list, max_length=10)
    statements: list[MeetingStatement] = Field(default_factory=list, max_length=40)
    status_signals: list[MeetingSignal] = Field(default_factory=list, max_length=30)
    decisions: list[MeetingDecision] = Field(default_factory=list, max_length=20)
    speaker_mapping: list[SpeakerProposal] = Field(default_factory=list, max_length=20)
    project_hint: str | None = Field(default=None, max_length=80)
    mentions: list[Mention] = Field(default_factory=list, max_length=30)
