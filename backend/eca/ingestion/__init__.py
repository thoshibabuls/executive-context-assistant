"""Sync orchestration, source items and the stage machine.

Owns (single writer, BACKEND_DESIGN.md §5.1): source_items.
Other modules import only from this package root.
"""

from eca.connectors import (
    ConnectorRegistry,
)  # re-exported for compositions (§5.2: connectors stay behind ingestion)
from eca.ingestion import tasks as _tasks  # registers handlers
from eca.ingestion.events import (
    SOURCE_ITEM_STAGE_DUE,
    SOURCE_ITEM_STORED,
    SYNC_REQUESTED,
    SourceItemStageDue,
    SourceItemStored,
    SyncRequested,
)
from eca.ingestion.service import (
    MAIL_RESOURCE,
    SourceItem,
    SyncReport,
    get_source_item,
    items_in_stage,
    reconcile_stages,
    reconcile_user_stages,
    set_stage,
    set_stage_error,
    store_messages,
    sync_mail,
)
from eca.ingestion.stages import STAGES, InvalidStageTransition, check_transition
from eca.ingestion.tasks import SYNC_HANDLER, periodic_tasks, request_syncs

del _tasks

__all__ = [
    "MAIL_RESOURCE",
    "SOURCE_ITEM_STAGE_DUE",
    "SOURCE_ITEM_STORED",
    "STAGES",
    "SYNC_HANDLER",
    "SYNC_REQUESTED",
    "ConnectorRegistry",
    "InvalidStageTransition",
    "SourceItem",
    "SourceItemStageDue",
    "SourceItemStored",
    "SyncReport",
    "SyncRequested",
    "check_transition",
    "get_source_item",
    "items_in_stage",
    "periodic_tasks",
    "reconcile_stages",
    "reconcile_user_stages",
    "request_syncs",
    "set_stage",
    "set_stage_error",
    "store_messages",
    "sync_mail",
]
