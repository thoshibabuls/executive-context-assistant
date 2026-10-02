"""AI-07 ``answer_synthesis`` output schema, version 1: the ``answer.v1`` schema of AI-06
(AI_PIPELINE.md §5.7), so grounding checks and rendering are the same for both roles."""

from __future__ import annotations

from eca.intelligence.output_schemas.answer_lookup import SCHEMA_VERSION, Answer, AnswerClaim, ClaimKind

__all__ = ["SCHEMA_VERSION", "Answer", "AnswerClaim", "ClaimKind"]
