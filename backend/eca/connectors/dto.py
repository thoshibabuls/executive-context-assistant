"""Provider-neutral objects returned by connectors (BACKEND_DESIGN.md §20.1).

Nothing outside ``eca.connectors`` sees provider payloads; core modules work with these DTOs.
Categories are neutral: inbox, sent, promotions, social, updates, trash, spam.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Generic, TypeVar

NEUTRAL_CATEGORIES = frozenset({"inbox", "sent", "promotions", "social", "updates", "trash", "spam"})


@dataclass(frozen=True)
class NormalizedPerson:
    email: str
    display_name: str | None = None
    provider_user_id: str | None = None


@dataclass(frozen=True)
class NormalizedMessage:
    external_id: str
    thread_external_id: str
    rfc822_id: str | None
    in_reply_to: str | None
    sent_at: datetime.datetime
    sender: NormalizedPerson
    to: tuple[NormalizedPerson, ...]
    cc: tuple[NormalizedPerson, ...]
    subject: str | None
    body_text: str | None
    body_html: str | None
    categories: tuple[str, ...]
    headers_subset: dict[str, str] = field(default_factory=dict)
    provider_version: str | None = None
    deep_link: str | None = None

    def __post_init__(self) -> None:
        if self.sent_at.tzinfo is None:
            raise ValueError("sent_at must be timezone-aware")
        unknown = set(self.categories) - NEUTRAL_CATEGORIES
        if unknown:
            raise ValueError(f"non-neutral categories from an adapter: {sorted(unknown)}")

    def content(self) -> dict[str, Any]:
        """The SOURCE content stored in ``source_items.content`` (BACKEND_DESIGN.md §17)."""
        data = asdict(self)
        data["sent_at"] = self.sent_at.isoformat()
        return {
            k: data[k]
            for k in (
                "rfc822_id",
                "in_reply_to",
                "sent_at",
                "sender",
                "to",
                "cc",
                "subject",
                "body_text",
                "body_html",
                "headers_subset",
            )
        }

    @property
    def content_hash(self) -> bytes:
        canonical = json.dumps(self.content(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).digest()


@dataclass(frozen=True)
class NormalizedAttendee:
    email: str
    display_name: str | None = None
    response: str | None = None  # accepted | declined | tentative | needs_action


@dataclass(frozen=True)
class NormalizedEvent:
    external_id: str
    series_external_id: str | None
    ical_uid: str | None
    start: datetime.datetime
    end: datetime.datetime
    timezone: str | None
    title: str | None
    description: str | None
    attendees: tuple[NormalizedAttendee, ...]
    organizer: NormalizedPerson | None
    conference_uri: str | None
    status: str  # confirmed | tentative | cancelled
    provider_version: str | None = None

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("event times must be timezone-aware")
        if self.end < self.start:
            raise ValueError("event ends before it starts")


T = TypeVar("T")


@dataclass(frozen=True)
class SyncBatch(Generic[T]):
    items: tuple[T, ...]
    deleted_external_ids: tuple[str, ...]
    next_page_token: str | None  # opaque; None when this run has no more pages
    high_water_cursor: str  # opaque cursor to store once the whole run is committed
