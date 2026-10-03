"""The code-defined retrievers a plan can name (CONTEXT_ARCHITECTURE.md §10; AI_PIPELINE.md §5.8).

The planner selects one of these by intent; it never builds a query of its own.
"""

from __future__ import annotations

from eca.retrieval.feed import what_changed, yesterday
from eca.retrieval.reply import reply_guidance
from eca.retrieval.retrievers import RETRIEVERS, Retriever
from eca.retrieval.topics import project_or_topic


def all_retrievers() -> dict[str, Retriever]:
    return {
        **RETRIEVERS,
        "what_changed": what_changed,
        "day_view": yesterday,
        "project": project_or_topic,
        "reply_guidance": reply_guidance,
    }
