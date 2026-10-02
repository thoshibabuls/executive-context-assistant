"""Events published by ``ingestion`` (BACKEND_DESIGN.md §5.1). IDs and small facts only."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from eca.platform.events import register_event

SOURCE_ITEM_STORED = "SourceItemStored"
SOURCE_ITEM_STAGE_DUE = "SourceItemStageDue"
SYNC_REQUESTED = "SyncRequested"
SOURCE_ITEM_DELETED = "SourceItemDeleted"


class SourceItemStored(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    source_item_id: UUID
    kind: str


class SourceItemStageDue(BaseModel):
    """Re-published by the reconciler for an item stuck past its stage SLA (§7.5)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    source_item_id: UUID
    stage: str


class SourceItemDeleted(BaseModel):
    """The provider permanently deleted the item, or a purge removed it (§9.3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    source_item_id: UUID
    kind: str


class SyncRequested(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    connection_id: UUID
    resource: str
    trigger: str  # manual | periodic | webhook


register_event(SOURCE_ITEM_STORED, SourceItemStored)
register_event(SOURCE_ITEM_STAGE_DUE, SourceItemStageDue)
register_event(SYNC_REQUESTED, SyncRequested)
register_event(SOURCE_ITEM_DELETED, SourceItemDeleted)
