"""Indexing, query planning, retrieval and packet assembly.

Owns (single writer, BACKEND_DESIGN.md §5.1): chunks, retrieval_traces. Reads the other modules
through their public APIs (§5.2). Other modules import only from this package root.
"""

from eca.retrieval import tasks as _tasks  # registers the index handlers
from eca.retrieval.chunking import ChunkDraft, calendar_chunks, email_chunks, estimate_tokens, split_text
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
from eca.retrieval.purge import purge_user
from eca.retrieval.tasks import INDEX_MEETING_HANDLER, INDEX_MESSAGE_HANDLER, INDEX_REMOVED_HANDLER

del _tasks

__all__ = [
    "INDEX_MEETING_HANDLER",
    "INDEX_MESSAGE_HANDLER",
    "INDEX_REMOVED_HANDLER",
    "AliasHit",
    "AliasMatcher",
    "AliasTarget",
    "ChunkDraft",
    "IndexReport",
    "IndexRetry",
    "IndexTarget",
    "ReembedReport",
    "calendar_chunks",
    "email_chunks",
    "estimate_tokens",
    "index_source",
    "meeting_target",
    "message_target",
    "purge_sources",
    "purge_unretained",
    "purge_user",
    "reembed_stale",
    "remove_source",
    "split_text",
]
