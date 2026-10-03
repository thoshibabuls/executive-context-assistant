"""Events published by ``people`` (BACKEND_DESIGN.md §5.1)."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from eca.platform.events import register_event

PERSON_CHANGED = "PersonChanged"


class PersonChanged(BaseModel):
    """A user correction of a person: edit, merge or alias (priority and profile recompute)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    person_id: UUID
    change: Literal["edit", "merge", "alias"]
    fields: tuple[str, ...] = ()
    merged_into_id: UUID | None = None


register_event(PERSON_CHANGED, PersonChanged)
