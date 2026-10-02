"""Connector protocols and registry (BACKEND_DESIGN.md §20).

A connector is built per connection by the registry from the connection's provider. Sync code
(``ingestion``) talks only to these protocols. Cursor and page tokens are opaque strings.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from eca.connectors.dto import NormalizedEvent, NormalizedMessage, SyncBatch


@dataclass(frozen=True)
class ConnectionInfo:
    connection_id: UUID
    user_id: UUID
    provider: str
    account_email: str


class MailConnector(Protocol):
    async def list_messages(
        self, *, cursor: str | None, page_token: str | None, now: datetime.datetime
    ) -> SyncBatch[NormalizedMessage]:
        """One page of messages changed since ``cursor`` (all messages when ``cursor`` is None).

        Raises ``CursorExpired`` when the provider no longer accepts ``cursor``.
        """
        ...


class CalendarConnector(Protocol):
    async def list_events(
        self, *, cursor: str | None, page_token: str | None, now: datetime.datetime
    ) -> SyncBatch[NormalizedEvent]: ...


MailFactory = Callable[[ConnectionInfo], MailConnector]
CalendarFactory = Callable[[ConnectionInfo], CalendarConnector]


class UnknownProvider(LookupError):
    pass


class ConnectorRegistry:
    """Provider → connector factories, configured by the composition (worker, tests)."""

    def __init__(self) -> None:
        self._mail: dict[str, MailFactory] = {}
        self._calendar: dict[str, CalendarFactory] = {}

    def register_mail(self, provider: str, factory: MailFactory) -> None:
        self._mail[provider] = factory

    def register_calendar(self, provider: str, factory: CalendarFactory) -> None:
        self._calendar[provider] = factory

    def mail(self, info: ConnectionInfo) -> MailConnector:
        try:
            return self._mail[info.provider](info)
        except KeyError:
            raise UnknownProvider(f"no mail connector for provider {info.provider!r}") from None

    def calendar(self, info: ConnectionInfo) -> CalendarConnector:
        try:
            return self._calendar[info.provider](info)
        except KeyError:
            raise UnknownProvider(f"no calendar connector for provider {info.provider!r}") from None
