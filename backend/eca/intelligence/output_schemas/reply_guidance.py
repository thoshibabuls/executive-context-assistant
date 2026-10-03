"""AI-08 ``reply_guidance`` output schema, version 1 (AI_PIPELINE.md §5.7, §5.9).

The answer schema's claim structure in three sections, plus a copy-only draft. Every section's
claims pass the grounding checks; the draft is always rendered as a suggestion.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from eca.intelligence.output_schemas.answer_lookup import AnswerClaim

SCHEMA_VERSION = "reply_guidance.v1"


class ReplyGuidance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answerable: bool
    context: list[AnswerClaim] = Field(default_factory=list, max_length=5)
    previous_agreement: list[AnswerClaim] = Field(default_factory=list, max_length=5)
    current_status: list[AnswerClaim] = Field(default_factory=list, max_length=5)
    draft: str | None = Field(default=None, max_length=2000)
    confidence: Literal["low", "medium", "high"]
    missing_info: str | None = Field(default=None, max_length=400)
