"""Events published by ``intelligence``."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from eca.platform.events import register_event

EXTRACTION_COMPLETED = "ExtractionCompleted"


class ExtractionCompleted(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    extraction_id: UUID
    source_item_id: UUID
    pipeline: str


register_event(EXTRACTION_COMPLETED, ExtractionCompleted)
