"""Conversations, messages and participants: normalization, reply state, triage projection.

Owns (single writer, BACKEND_DESIGN.md §5.1): conversations, messages, message_participants.
Other modules import only from this package root.
"""

from eca.communication import tasks as _tasks  # registers handlers
from eca.communication.cleaning import clean_body, html_to_text, split_forwarded
from eca.communication.events import MESSAGE_NORMALIZED, MessageNormalized
from eca.communication.prefilter import PrefilterDecision, PrefilterInput, decide
from eca.communication.purge import (
    PREFILTERED_BODY_DAYS,
    RELEVANT_BODY_DAYS,
    conversation_ids_for_sources,
    detach_connection,
    purge_bodies,
    purge_sources,
    purge_user,
    sources_without_body,
)
from eca.communication.queries import (
    ConversationPage,
    ConversationSummary,
    MessageSummary,
    PriorityInput,
    conversation_detail,
    conversations_by_ids,
    mark_handled,
    message_conversation_id,
    needs_response_page,
    priority_inputs,
    set_conversation_priority,
    set_priority_override,
)
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
    "PREFILTERED_BODY_DAYS",
    "RELEVANT_BODY_DAYS",
    "ConversationPage",
    "ConversationSummary",
    "MessageNormalized",
    "MessageSummary",
    "MessageView",
    "NormalizeResult",
    "PrefilterDecision",
    "PrefilterInput",
    "PriorityInput",
    "clean_body",
    "conversation_detail",
    "conversation_ids_for_sources",
    "conversation_source_items",
    "conversations_by_ids",
    "decide",
    "detach_connection",
    "get_message_view",
    "html_to_text",
    "mark_conversation_handled",
    "mark_handled",
    "message_conversation_id",
    "needs_response_page",
    "normalize_source_item",
    "participants_of_conversation",
    "priority_inputs",
    "purge_bodies",
    "purge_sources",
    "purge_user",
    "recompute_reply_state",
    "set_conversation_priority",
    "set_priority_override",
    "set_triage_projection",
    "sources_without_body",
    "split_forwarded",
]
