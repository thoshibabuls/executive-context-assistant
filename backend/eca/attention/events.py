"""Events published by ``attention`` (BACKEND_DESIGN.md §5.1)."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from eca.platform.events import register_event

REMINDER_DUE = "ReminderDue"


class ReminderDue(BaseModel):
    """A proactive delivery created a ``web_push`` notification (sent by ``attention.web_push``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    reminder_id: UUID
    seq: int


register_event(REMINDER_DUE, ReminderDue)
