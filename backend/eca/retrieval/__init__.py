"""Indexing, query planning, retrieval and packet assembly.

Owns (single writer, BACKEND_DESIGN.md §5.1): chunks, retrieval_traces. Reads the other modules
through their public APIs (§5.2). Other modules import only from this package root.
"""

from eca.retrieval import tasks as _tasks  # registers the index handlers
from eca.retrieval.assembly import (
    Assembly,
    assemble,
    context_for,
    embed_question,
    needs_discovery,
    plan_window,
)
from eca.retrieval.cards import delimit, fmt_date
from eca.retrieval.changes import ChangeSet, DayView, NetChange, changes_since, day_view, item_net_change
from eca.retrieval.chunking import ChunkDraft, calendar_chunks, email_chunks, estimate_tokens, split_text
from eca.retrieval.coverage import Coverage, SourceCoverage, coverage_sentence
from eca.retrieval.feed import ChangeEntry, ChangeFeedPage, DayViewPage, change_feed, day_view_for
from eca.retrieval.indexing import (
    IndexReport,
    IndexRetry,
    IndexTarget,
    ReembedReport,
    index_source,
    meeting_target,
    message_target,
    purge_sources,
    purge_unretained,
    reembed_stale,
    remove_source,
)
from eca.retrieval.mentions import AliasHit, AliasMatcher, AliasTarget
from eca.retrieval.packet import BUDGETS, COVERAGE_CID, Budget, Packet, PacketItem
from eca.retrieval.plan import (
    INTENT_SCENARIO,
    INTENT_TIER,
    LIST_INTENTS,
    FocusEntry,
    Plan,
    SessionScope,
    SessionState,
    Turn,
)
from eca.retrieval.purge import purge_user
from eca.retrieval.registry import all_retrievers
from eca.retrieval.search import ChunkHit, SearchFilters, hybrid_search, ts_terms
from eca.retrieval.tasks import INDEX_MEETING_HANDLER, INDEX_MESSAGE_HANDLER, INDEX_REMOVED_HANDLER
from eca.retrieval.temporal import TimeWindow
from eca.retrieval.topics import (
    TOPIC_LABEL,
    ProjectContextPage,
    TopicGroup,
    project_context_for,
    topic_groups,
    topic_mode,
)
from eca.retrieval.traces import purge_old_traces

del _tasks

__all__ = [
    "BUDGETS",
    "COVERAGE_CID",
    "INDEX_MEETING_HANDLER",
    "INDEX_MESSAGE_HANDLER",
    "INDEX_REMOVED_HANDLER",
    "INTENT_SCENARIO",
    "INTENT_TIER",
    "LIST_INTENTS",
    "TOPIC_LABEL",
    "AliasHit",
    "AliasMatcher",
    "AliasTarget",
    "Assembly",
    "Budget",
    "ChangeEntry",
    "ChangeFeedPage",
    "ChangeSet",
    "ChunkDraft",
    "ChunkHit",
    "Coverage",
    "DayView",
    "DayViewPage",
    "FocusEntry",
    "IndexReport",
    "IndexRetry",
    "IndexTarget",
    "NetChange",
    "Packet",
    "PacketItem",
    "Plan",
    "ProjectContextPage",
    "ReembedReport",
    "SearchFilters",
    "SessionScope",
    "SessionState",
    "SourceCoverage",
    "TimeWindow",
    "TopicGroup",
    "Turn",
    "all_retrievers",
    "assemble",
    "calendar_chunks",
    "change_feed",
    "changes_since",
    "context_for",
    "coverage_sentence",
    "day_view",
    "day_view_for",
    "delimit",
    "email_chunks",
    "embed_question",
    "estimate_tokens",
    "fmt_date",
    "hybrid_search",
    "index_source",
    "item_net_change",
    "meeting_target",
    "message_target",
    "needs_discovery",
    "plan_window",
    "project_context_for",
    "purge_old_traces",
    "purge_sources",
    "purge_unretained",
    "purge_user",
    "reembed_stale",
    "remove_source",
    "split_text",
    "topic_groups",
    "topic_mode",
    "ts_terms",
]
