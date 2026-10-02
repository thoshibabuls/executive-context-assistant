"""Connections to external providers and their sync cursors.

Owns (single writer, BACKEND_DESIGN.md §5.1): connections, sync_cursors. Google connect,
envelope-encrypted tokens, scopes and disconnect (slice 1.2); fake connections for tests.
Other modules import only from this package root.
"""

from eca.connections.events import CONNECTION_STATUS_CHANGED, ConnectionStatusChanged
from eca.connections.google import (
    ConnectionView,
    GoogleOAuthConfig,
    access_token,
    capabilities_for,
    complete_connect,
    disconnect,
    list_connections,
    start_connect,
)
from eca.connections.purge import purge_connection, purge_user, revoke_all_tokens
from eca.connections.service import (
    CursorState,
    acquire_lease,
    advance_cursor,
    create_connection,
    cursor_obtained_at,
    get_connection,
    get_cursor,
    list_active_connections,
    release_after_failure,
    reset_cursor,
    save_page_token,
)
from eca.connections.tokens import TokenCrypto

__all__ = [
    "CONNECTION_STATUS_CHANGED",
    "ConnectionStatusChanged",
    "ConnectionView",
    "CursorState",
    "GoogleOAuthConfig",
    "TokenCrypto",
    "access_token",
    "acquire_lease",
    "advance_cursor",
    "capabilities_for",
    "complete_connect",
    "create_connection",
    "cursor_obtained_at",
    "disconnect",
    "get_connection",
    "get_cursor",
    "list_active_connections",
    "list_connections",
    "purge_connection",
    "purge_user",
    "release_after_failure",
    "reset_cursor",
    "revoke_all_tokens",
    "save_page_token",
    "start_connect",
]
