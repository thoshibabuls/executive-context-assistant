"""Sync orchestration, source items and the stage machine.

Owns (single writer, BACKEND_DESIGN.md §5.1): source_items.
Other modules import only from this package root.
"""

from eca.connectors import (
    ConnectorRegistry,
)  # re-exported for compositions (§5.2: connectors stay behind ingestion)
from eca.ingestion import tasks as _tasks  # registers handlers
from eca.ingestion.events import (
    SOURCE_ITEM_DELETED,
    SOURCE_ITEM_STAGE_DUE,
    SOURCE_ITEM_STORED,
    SYNC_REQUESTED,
    SourceItemDeleted,
    SourceItemStageDue,
    SourceItemStored,
    SyncRequested,
)
from eca.ingestion.purge import purge_sources, purge_user, source_ids_for_connection
from eca.ingestion.registry import build_connector_registry
from eca.ingestion.service import (
    CALENDAR_RESOURCE,
    MAIL_RESOURCE,
    SourceItem,
    SyncReport,
    event_content,
    get_source_item,
    items_in_stage,
    mark_deleted,
    reconcile_stages,
    reconcile_user_stages,
    set_stage,
    set_stage_error,
    store_events,
    store_messages,
    sync_calendar,
    sync_mail,
    visible_among,
    visible_source_ids,
)
from eca.ingestion.stages import STAGES, InvalidStageTransition, check_transition
from eca.ingestion.tasks import SYNC_HANDLER, periodic_tasks, request_syncs

del _tasks

__all__ = [
    "CALENDAR_RESOURCE",
    "MAIL_RESOURCE",
    "SOURCE_ITEM_DELETED",
    "SOURCE_ITEM_STAGE_DUE",
    "SOURCE_ITEM_STORED",
    "STAGES",
    "SYNC_HANDLER",
    "SYNC_REQUESTED",
    "ConnectorRegistry",
    "InvalidStageTransition",
    "SourceItem",
    "SourceItemDeleted",
    "SourceItemStageDue",
    "SourceItemStored",
    "SyncReport",
    "SyncRequested",
    "build_connector_registry",
    "check_transition",
    "event_content",
    "get_source_item",
    "items_in_stage",
    "mark_deleted",
    "periodic_tasks",
    "purge_sources",
    "purge_user",
    "reconcile_stages",
    "reconcile_user_stages",
    "request_syncs",
    "set_stage",
    "set_stage_error",
    "source_ids_for_connection",
    "store_events",
    "store_messages",
    "sync_calendar",
    "sync_mail",
    "visible_among",
    "visible_source_ids",
]
