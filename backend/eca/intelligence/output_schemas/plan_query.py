"""AI-05 ``plan_query`` output schema, version 1 (AI_PIPELINE.md §5.8).

The model only classifies the question into one of the code-defined retrievers and copies names,
the topic and the time phrase; it never answers and never writes a query.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "plan_query.v1"

PlanIntent = Literal[
    "waiting_for",
    "promised",
    "who_waiting_on_me",
    "needs_response",
    "overdue",
    "deadlines",
    "person",
    "topic_status",
    "project",
    "day_view",
    "what_changed",
    "next_action",
    "email_context",
    "unsupported",
]
TimeExpression = Literal[
    "today", "yesterday", "this_week", "last_week", "recently", "since_last_meeting", "since_date"
]


class PlanQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: PlanIntent
    person_names: list[str] = Field(default_factory=list, max_length=3)
    topic: str | None = Field(default=None, max_length=120)
    time_expression: TimeExpression | None = None
    since_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    needs_clarification: bool = False
    confidence: float = Field(ge=0.0, le=1.0)
