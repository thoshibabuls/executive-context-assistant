"""Events published by ``communication``."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from eca.platform.events import register_event

MESSAGE_NORMALIZED = "MessageNormalized"
CONVERSATION_STATE_CHANGED = "ConversationStateChanged"


class MessageNormalized(BaseModel):
    """A relevant message is ready for extraction (stage ``extract_pending``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    message_id: UUID
    source_item_id: UUID


class ConversationStateChanged(BaseModel):
    """A conversation's reply state changed without a new message (Trash/Spam, restore,
    provider deletion; BACKEND_DESIGN.md §9.2, §9.3 step 5): priority and reminders follow."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    conversation_id: UUID
    reason: str  # trashed | restored | deleted


register_event(MESSAGE_NORMALIZED, MessageNormalized)
register_event(CONVERSATION_STATE_CHANGED, ConversationStateChanged)
