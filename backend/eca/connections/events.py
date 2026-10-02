"""Events published by ``connections``."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from eca.platform.events import register_event

CONNECTION_STATUS_CHANGED = "ConnectionStatusChanged"


class ConnectionStatusChanged(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    connection_id: UUID
    status: str


register_event(CONNECTION_STATUS_CHANGED, ConnectionStatusChanged)
