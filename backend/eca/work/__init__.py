"""Context core: work items, decisions, evidence, context events, fold.

Owns (single writer, BACKEND_DESIGN.md §5.1): work_items, work_item_owners, decisions,
evidence, item_evidence, context_events, entity_links. Orchestrates email extraction and apply
(§5.6). Other modules import only from this package root.
"""

from eca.work import pipeline as _pipeline  # registers the extract and apply handlers
from eca.work.apply import ApplyReport, apply_extraction
from eca.work.confidence import Confidence, band, compute
from eca.work.dates import DueResolution, compatible_due, resolve_due
from eca.work.events import ADJUDICATION_NEEDED, WORK_ITEM_CHANGED, AdjudicationNeeded, WorkItemChanged
from eca.work.fold import FoldEvent, FoldResult, fold
from eca.work.mapping import Mapped, authority, map_statement
from eca.work.pipeline import (
    APPLY_HANDLER,
    EXTRACT_HANDLER,
    ExtractionRetry,
    extract_source_item,
)
from eca.work.recompute import RecomputeReport, reapply_all, refold_all
from eca.work.service import (
    AppendResult,
    WorkItemView,
    add_note,
    append_event,
    confirm,
    count_items,
    create_user_item,
    evidence_id_for,
    get_item,
    list_items,
    reject,
    set_lifecycle,
    timeline,
    user_edit,
)

del _pipeline

__all__ = [
    "ADJUDICATION_NEEDED",
    "APPLY_HANDLER",
    "EXTRACT_HANDLER",
    "WORK_ITEM_CHANGED",
    "AdjudicationNeeded",
    "AppendResult",
    "ApplyReport",
    "Confidence",
    "DueResolution",
    "ExtractionRetry",
    "FoldEvent",
    "FoldResult",
    "Mapped",
    "RecomputeReport",
    "WorkItemChanged",
    "WorkItemView",
    "add_note",
    "append_event",
    "apply_extraction",
    "authority",
    "band",
    "compatible_due",
    "compute",
    "confirm",
    "count_items",
    "create_user_item",
    "evidence_id_for",
    "extract_source_item",
    "fold",
    "get_item",
    "list_items",
    "map_statement",
    "reapply_all",
    "refold_all",
    "reject",
    "resolve_due",
    "set_lifecycle",
    "timeline",
    "user_edit",
]
