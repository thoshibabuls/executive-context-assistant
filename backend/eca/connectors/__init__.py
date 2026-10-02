"""Provider adapters behind neutral protocols (BACKEND_DESIGN.md §20).

Owns no tables. Imported only by ``ingestion`` and ``connections`` (import-linter contract), and
by composition packages and the evaluation harness that configure the connector registry.
"""

from eca.connectors.dto import (
    NEUTRAL_CATEGORIES,
    NormalizedAttendee,
    NormalizedEvent,
    NormalizedMessage,
    NormalizedPerson,
    SyncBatch,
)
from eca.connectors.fake import (
    FakeAccounts,
    FakeCalendarConnector,
    FakeConnectorFailure,
    FakeFeed,
    FakeMailConnector,
    feed_of,
    load_eml_dir,
    parse_eml,
)
from eca.connectors.protocols import (
    CalendarConnector,
    ConnectionInfo,
    ConnectorRegistry,
    MailConnector,
    UnknownProvider,
)

__all__ = [
    "NEUTRAL_CATEGORIES",
    "CalendarConnector",
    "ConnectionInfo",
    "ConnectorRegistry",
    "FakeAccounts",
    "FakeCalendarConnector",
    "FakeConnectorFailure",
    "FakeFeed",
    "FakeMailConnector",
    "MailConnector",
    "NormalizedAttendee",
    "NormalizedEvent",
    "NormalizedMessage",
    "NormalizedPerson",
    "SyncBatch",
    "UnknownProvider",
    "feed_of",
    "load_eml_dir",
    "parse_eml",
]
