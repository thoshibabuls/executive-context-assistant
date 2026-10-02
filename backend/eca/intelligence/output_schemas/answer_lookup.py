"""AI-06 ``answer_lookup`` output schema, version 1 (AI_PIPELINE.md §5.7, §5.8).

Shared by AI-07 (``answer_synthesis``): every claim has a kind and cites packet IDs (``S<n>``)
or ``COVERAGE``. The application renders the displayed answer from the verified claims.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "answer.v1"

ClaimKind = Literal["source", "user", "inference", "recommendation", "absence"]


class AnswerClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=400)
    citations: list[str] = Field(default_factory=list, max_length=8)
    kind: ClaimKind


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answerable: bool
    answer_markdown: str = Field(default="", max_length=4000)
    claims: list[AnswerClaim] = Field(default_factory=list, max_length=20)
    confidence: Literal["low", "medium", "high"]
    missing_info: str | None = Field(default=None, max_length=400)
