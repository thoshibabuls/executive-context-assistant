"""Chat sessions, grounded answers and (Phase 3) reply guidance with copy-only drafts.

Owns (single writer, BACKEND_DESIGN.md §5.1): chat_sessions, chat_messages. Depends on
``retrieval`` (planning, packets), ``intelligence`` (AI-05/06/07 calls, budgets) and ``identity``
(checkpoints) only (§5.2). Other modules import only from this package root.
"""

from eca.chat.grounding import Verified, VerifiedClaim, checked_tokens, tokens, verify
from eca.chat.render import (
    ABSTAIN,
    DEGRADED,
    Rendered,
    citation_snapshots,
    render_abstention,
    render_degraded,
    render_list,
    render_verified,
)
from eca.chat.reply import GuidanceReplay, GuidanceStart, guidance_replay_events, run_guidance, start_guidance
from eca.chat.sessions import (
    AssistantMessage,
    MessageView,
    SessionView,
    create_session,
    delete_session,
    get_message,
    get_session,
    list_sessions,
    load_state,
    merge_focus,
    messages_of,
    purge_user,
    record_message_feedback,
)
from eca.chat.turn import ChatEvent, Replay, TurnStart, message_payload, replay_events, run_turn, start_turn

__all__ = [
    "ABSTAIN",
    "DEGRADED",
    "AssistantMessage",
    "ChatEvent",
    "GuidanceReplay",
    "GuidanceStart",
    "MessageView",
    "Rendered",
    "Replay",
    "SessionView",
    "TurnStart",
    "Verified",
    "VerifiedClaim",
    "checked_tokens",
    "citation_snapshots",
    "create_session",
    "delete_session",
    "get_message",
    "get_session",
    "guidance_replay_events",
    "list_sessions",
    "load_state",
    "merge_focus",
    "message_payload",
    "messages_of",
    "purge_user",
    "record_message_feedback",
    "render_abstention",
    "render_degraded",
    "render_list",
    "render_verified",
    "replay_events",
    "run_guidance",
    "run_turn",
    "start_guidance",
    "start_turn",
    "tokens",
    "verify",
]
