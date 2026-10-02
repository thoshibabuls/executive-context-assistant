"""Messages, conversations and reply state.

Owns (single writer, BACKEND_DESIGN.md §5.1): conversations, messages, message_participants.
Implemented in slice 1.3; other modules import only from this package root.
"""
