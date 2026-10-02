"""Conversations, messages and participants: normalization, reply state, triage projection.

Owns (single writer, BACKEND_DESIGN.md §5.1): conversations, messages, message_participants.
Other modules import only from this package root.
"""

from eca.communication import tasks as _tasks  # registers handlers
from eca.communication.cleaning import clean_body, html_to_text, split_forwarded
from eca.communication.events import MESSAGE_NORMALIZED, MessageNormalized
from eca.communication.prefilter import PrefilterDecision, PrefilterInput, decide
from eca.communication.service import (
    MessageView,
    NormalizeResult,
    conversation_source_items,
    get_message_view,
    mark_conversation_handled,
    normalize_source_item,
    participants_of_conversation,
    recompute_reply_state,
    set_triage_projection,
)
from eca.communication.tasks import NORMALIZE_HANDLER, NORMALIZE_RETRY_HANDLER

del _tasks

__all__ = [
    "MESSAGE_NORMALIZED",
    "NORMALIZE_HANDLER",
    "NORMALIZE_RETRY_HANDLER",
    "MessageNormalized",
    "MessageView",
    "NormalizeResult",
    "PrefilterDecision",
    "PrefilterInput",
    "clean_body",
    "conversation_source_items",
    "decide",
    "get_message_view",
    "html_to_text",
    "mark_conversation_handled",
    "normalize_source_item",
    "participants_of_conversation",
    "recompute_reply_state",
    "set_triage_projection",
    "split_forwarded",
]
