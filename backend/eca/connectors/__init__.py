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
from eca.connectors.gmail import GmailConnector, QuotaBucket, categories_from_labels
from eca.connectors.google_auth import (
    CALENDAR_READONLY,
    CAPABILITY_SCOPES,
    GMAIL_READONLY,
    AccessToken,
    refresh_access_token,
    revoke_token,
)
from eca.connectors.protocols import (
    CalendarConnector,
    ConnectionInfo,
    ConnectorRegistry,
    MailConnector,
    UnknownProvider,
)

__all__ = [
    "CALENDAR_READONLY",
    "CAPABILITY_SCOPES",
    "GMAIL_READONLY",
    "NEUTRAL_CATEGORIES",
    "AccessToken",
    "CalendarConnector",
    "ConnectionInfo",
    "ConnectorRegistry",
    "FakeAccounts",
    "FakeCalendarConnector",
    "FakeConnectorFailure",
    "FakeFeed",
    "FakeMailConnector",
    "GmailConnector",
    "MailConnector",
    "NormalizedAttendee",
    "NormalizedEvent",
    "NormalizedMessage",
    "NormalizedPerson",
    "QuotaBucket",
    "SyncBatch",
    "UnknownProvider",
    "categories_from_labels",
    "feed_of",
    "load_eml_dir",
    "parse_eml",
    "refresh_access_token",
    "revoke_token",
]
