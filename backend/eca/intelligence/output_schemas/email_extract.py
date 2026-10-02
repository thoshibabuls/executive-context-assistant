"""AI-01 ``email_extract`` output schema, version 1 (AI_PIPELINE.md §5.2).

The model labels statements; code maps them to types, directions, dates and confidence
(§5.3-§5.5). ``due_iso_guess`` is diagnostic only and is never stored as a date.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "email_extract.v1"

StatementKind = Literal[
    "promise", "request", "acceptance", "report_commitment", "report_request", "assignment"
]
Signal = Literal[
    "progress", "completed_claim", "delay", "new_deadline", "cancelled", "resolved", "accepted", "declined"
]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Triage(_Model):
    category: Literal["action", "fyi", "scheduling", "newsletter", "notification", "personal", "other"]
    needs_reply: bool
    request_type: Literal["reply", "review", "approve", "decide", "provide_info", "none"]
    urgency_signals: list[Literal["deadline", "explicit_urgency", "escalation"]] = Field(default_factory=list)
    business_impact: Literal["low", "medium", "high"]
    impact_reason: str = Field(default="", max_length=200)
    confidence: float = Field(ge=0.0, le=1.0)


class Statement(_Model):
    statement_kind: StatementKind
    action: str = Field(min_length=1, max_length=120)
    speaker: Literal["sender"] = "sender"
    owner_ref: str = Field(pattern=r"^(speaker|self|sender|unknown|recipient:.+|name:.+)$")
    beneficiary_ref: str | None = Field(default=None, pattern=r"^(self|sender|unknown|recipient:.+|name:.+)$")
    due_text: str | None = Field(default=None, max_length=80)
    due_iso_guess: str | None = None
    is_deadline_only: bool = False
    in_forwarded_content: bool = False
    forwarded_author_ref: str | None = None
    candidate_id: str | None = Field(default=None, pattern=r"^C[1-8]$")
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_quote: str = Field(min_length=1, max_length=600)


class StatusSignal(_Model):
    candidate_id: str = Field(pattern=r"^C[1-8]$")
    signal: Signal
    new_due_text: str | None = Field(default=None, max_length=80)
    evidence_quote: str = Field(min_length=1, max_length=600)
    confidence: float = Field(ge=0.0, le=1.0)


class DecisionOut(_Model):
    kind: Literal["decision", "open_question"]
    statement: str = Field(min_length=1, max_length=300)
    evidence_quote: str = Field(min_length=1, max_length=600)
    confidence: float = Field(ge=0.0, le=1.0)


class Mention(_Model):
    surface_text: str = Field(min_length=1, max_length=120)
    type: Literal["person", "organization", "project"]


class EmailExtraction(_Model):
    gist: str = Field(max_length=200)
    triage: Triage
    statements: list[Statement] = Field(default_factory=list, max_length=20)
    status_signals: list[StatusSignal] = Field(default_factory=list, max_length=20)
    decisions: list[DecisionOut] = Field(default_factory=list, max_length=10)
    project_hint: str | None = Field(default=None, max_length=80)
    mentions: list[Mention] = Field(default_factory=list, max_length=30)
