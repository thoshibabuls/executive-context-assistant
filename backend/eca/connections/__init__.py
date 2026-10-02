"""Connections to external providers and their sync cursors.

Owns (single writer, BACKEND_DESIGN.md §5.1): connections, sync_cursors. Batch A: data model,
fake connections and cursor rules; the Google connect flow and tokens are Batch B.
Other modules import only from this package root.
"""

from eca.connections.service import (
    CursorState,
    acquire_lease,
    advance_cursor,
    create_connection,
    get_connection,
    get_cursor,
    list_active_connections,
    release_after_failure,
    reset_cursor,
    save_page_token,
)

__all__ = [
    "CursorState",
    "acquire_lease",
    "advance_cursor",
    "create_connection",
    "get_connection",
    "get_cursor",
    "list_active_connections",
    "release_after_failure",
    "reset_cursor",
    "save_page_token",
]
