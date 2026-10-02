"""Users, sessions, preferences, checkpoints.

Owns (single writer, BACKEND_DESIGN.md §5.1): users, auth_sessions, user_preferences, user_checkpoints.
Implemented in slice 1.1; other modules import only from this package root.
"""
