"""AI-03 ``thread_summary`` output schema, version 1 (AI_PIPELINE.md §5.9).

Narrative only: a short summary and key points citing message references (``M<n>``). The
application drops key points whose references do not exist and stores the result with its
provenance (``conversations.summary_*``).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "thread_summary.v1"


class KeyPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=300)
    messages: list[str] = Field(default_factory=list, max_length=10)


class ThreadSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=900)
    key_points: list[KeyPoint] = Field(default_factory=list, max_length=6)
