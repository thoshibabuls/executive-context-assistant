"""Events published by ``work``."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from eca.platform.events import register_event

WORK_ITEM_CHANGED = "WorkItemChanged"
ADJUDICATION_NEEDED = "AdjudicationNeeded"


class WorkItemChanged(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    work_item_id: UUID
    version: int
    event_type: str


class AdjudicationNeeded(BaseModel):
    """Apply found an ambiguous candidate (§8.3); AI-02 runs only when enabled (experiment X1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    work_item_id: UUID
    extraction_id: UUID
    statement_index: int


register_event(WORK_ITEM_CHANGED, WorkItemChanged)
register_event(ADJUDICATION_NEEDED, AdjudicationNeeded)
