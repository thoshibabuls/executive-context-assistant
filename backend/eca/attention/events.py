"""Events published by ``attention`` (BACKEND_DESIGN.md §5.1)."""

from __future__ import annotations

import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from eca.platform.events import register_event

REMINDER_DUE = "ReminderDue"
BRIEFING_DUE = "BriefingDue"


class ReminderDue(BaseModel):
    """A proactive delivery created a ``web_push`` notification (sent by ``attention.web_push``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    reminder_id: UUID
    seq: int


class BriefingDue(BaseModel):
    """Time to generate the user's daily briefing for a local date (``daily_briefing``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    date: datetime.date


register_event(REMINDER_DUE, ReminderDue)
register_event(BRIEFING_DUE, BriefingDue)
