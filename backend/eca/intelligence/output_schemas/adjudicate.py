"""AI-02 ``adjudicate`` output schema, version 1 (disabled until experiment X1)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "adjudicate.v1"


class Adjudication(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_commitment: bool
    statement_kind: Literal[
        "promise", "request", "acceptance", "report_commitment", "report_request", "assignment"
    ]
    owner_ref: str
    same_as_candidate: str | None = Field(default=None, pattern=r"^C[1-8]$")
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_quote: str = Field(min_length=1, max_length=600)
