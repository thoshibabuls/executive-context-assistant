"""Events published by ``retrieval`` (slice 3.2)."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from eca.platform.events import register_event

THREAD_SUMMARY_DUE = "ThreadSummaryDue"


class ThreadSummaryDue(BaseModel):
    """A thread was claimed for AI-03, keyed by its newest relevant message (BACKEND_DESIGN.md §15)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    conversation_id: UUID
    through_message_id: UUID
    requested: bool = False


register_event(THREAD_SUMMARY_DUE, ThreadSummaryDue)
