"""Indexing, query planning, retrieval and packet assembly.

Owns (single writer, BACKEND_DESIGN.md §5.1): chunks, retrieval_traces. Reads the other modules
through their public APIs (§5.2). Other modules import only from this package root.
"""

from eca.retrieval import tasks as _tasks  # registers the index handlers
from eca.retrieval.assembly import Assembly, assemble, embed_question, needs_discovery, plan_window
from eca.retrieval.cards import delimit, fmt_date
from eca.retrieval.chunking import ChunkDraft, calendar_chunks, email_chunks, estimate_tokens, split_text
from eca.retrieval.coverage import Coverage, SourceCoverage, coverage_sentence
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
from eca.retrieval.search import ChunkHit, SearchFilters, hybrid_search, ts_terms
from eca.retrieval.tasks import INDEX_MEETING_HANDLER, INDEX_MESSAGE_HANDLER, INDEX_REMOVED_HANDLER
from eca.retrieval.temporal import TimeWindow
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
    "AliasHit",
    "AliasMatcher",
    "AliasTarget",
    "Assembly",
    "Budget",
    "ChunkDraft",
    "ChunkHit",
    "Coverage",
    "FocusEntry",
    "IndexReport",
    "IndexRetry",
    "IndexTarget",
    "Packet",
    "PacketItem",
    "Plan",
    "ReembedReport",
    "SearchFilters",
    "SessionScope",
    "SessionState",
    "SourceCoverage",
    "TimeWindow",
    "Turn",
    "assemble",
    "calendar_chunks",
    "coverage_sentence",
    "delimit",
    "email_chunks",
    "embed_question",
    "estimate_tokens",
    "fmt_date",
    "hybrid_search",
    "index_source",
    "meeting_target",
    "message_target",
    "needs_discovery",
    "plan_window",
    "purge_old_traces",
    "purge_sources",
    "purge_unretained",
    "purge_user",
    "reembed_stale",
    "remove_source",
    "split_text",
    "ts_terms",
]
