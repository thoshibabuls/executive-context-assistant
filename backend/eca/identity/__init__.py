"""Users, sessions, preferences and checkpoints.

Owns (single writer, BACKEND_DESIGN.md §5.1): users, auth_sessions, user_preferences, user_checkpoints.
Batch A implements the ``users`` table and its service functions; sessions and sign-in are Batch B.
Other modules import only from this package root.
"""

from eca.identity.service import UserSettings, create_user, get_user_settings, list_active_user_ids

__all__ = ["UserSettings", "create_user", "get_user_settings", "list_active_user_ids"]
