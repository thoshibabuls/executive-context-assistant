"""Indexing, query planning, retrieval and packet assembly.

Owns (single writer, BACKEND_DESIGN.md §5.1): chunks, retrieval_traces.
Implemented in Phase 2; other modules import only from this package root.
"""
