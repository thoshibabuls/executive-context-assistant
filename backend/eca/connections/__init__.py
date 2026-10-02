"""OAuth grants, encrypted tokens and sync cursors.

Owns (single writer, BACKEND_DESIGN.md §5.1): connections, sync_cursors.
Implemented in slice 1.2; other modules import only from this package root.
"""
