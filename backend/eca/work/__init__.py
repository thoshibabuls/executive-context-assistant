"""Context core: work items, decisions, evidence, context events, fold.

Owns (single writer, BACKEND_DESIGN.md §5.1): work_items, work_item_owners, decisions,
evidence, item_evidence, context_events, entity_links.
Implemented in slice 1.4; other modules import only from this package root.
"""
