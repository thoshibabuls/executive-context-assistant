"""Events published by ``communication``."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from eca.platform.events import register_event

MESSAGE_NORMALIZED = "MessageNormalized"


class MessageNormalized(BaseModel):
    """A relevant message is ready for extraction (stage ``extract_pending``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    message_id: UUID
    source_item_id: UUID


register_event(MESSAGE_NORMALIZED, MessageNormalized)
