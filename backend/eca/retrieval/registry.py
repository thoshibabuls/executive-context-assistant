"""The code-defined retrievers a plan can name (CONTEXT_ARCHITECTURE.md §10; AI_PIPELINE.md §5.8).

The planner selects one of these by intent; it never builds a query of its own.
"""

from __future__ import annotations

from eca.retrieval.feed import what_changed, yesterday
from eca.retrieval.retrievers import RETRIEVERS, Retriever


def all_retrievers() -> dict[str, Retriever]:
    return {**RETRIEVERS, "what_changed": what_changed, "day_view": yesterday}
