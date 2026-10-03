"""Fake mail and calendar connectors (slice 1.3), reading synthetic ``world_v1`` data.

They behave like a provider adapter: opaque cursors and page tokens, items that become visible
when their delivery time has passed (simulated clocks), neutral categories, and injectable
failures (a crash after N pages for RT-06, expired cursors). Positions are stable because items
are ordered by delivery time and then by insertion order, never re-sorted later.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from typing import Generic, TypeVar

from eca.connectors.dto import (
    NormalizedEvent,
    NormalizedMessage,
    NormalizedPerson,
    SyncBatch,
)
from eca.connectors.protocols import ConnectionInfo
from eca.platform.errors import CursorExpired

T = TypeVar("T")
_EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.UTC)
HEADERS_KEPT = (
    "List-Unsubscribe",
    "List-Id",
    "Precedence",
    "Auto-Submitted",
    "X-Autoreply",
    "X-Auto-Response-Suppress",
)


class FakeConnectorFailure(RuntimeError):
    """Injected provider failure (network error, process crash simulation)."""


@dataclass
class _Delivered(Generic[T]):
    item: T
    delivered_at: datetime.datetime


@dataclass
class FakeFeed(Generic[T]):
    """An append-only feed of items with delivery times; shared by connectors of one account."""

    page_size: int = 20
    _items: list[_Delivered[T]] = field(default_factory=list)
    fail_after_pages: int | None = None  # raise once after serving this many pages
    expire_cursors_below: int = 0  # cursors < this raise CursorExpired
    pages_served: int = 0
    pending_deletions: list[str] = field(default_factory=list)  # reported once, on the next page

    def delete(self, external_id: str) -> None:
        """The provider permanently deletes an item: the next sync reports its ID as deleted."""
        self._items = [d for d in self._items if getattr(d.item, "external_id", None) != external_id]
        self.pending_deletions.append(external_id)

    def add(self, item: T, *, delivered_at: datetime.datetime | None = None) -> None:
        when = delivered_at or _EPOCH
        if when.tzinfo is None:
            raise ValueError("delivered_at must be timezone-aware")
        self._items.append(_Delivered(item, when))

    def extend(self, items: Iterable[T]) -> None:
        for item in items:
            self.add(item)

    def visible(self, now: datetime.datetime) -> list[T]:
        ordered = sorted(enumerate(self._items), key=lambda p: (p[1].delivered_at, p[0]))
        return [d.item for _, d in ordered if d.delivered_at <= now]

    def page(self, *, cursor: str | None, page_token: str | None, now: datetime.datetime) -> SyncBatch[T]:
        start_cursor = int(cursor) if cursor else 0
        if cursor is not None and start_cursor < self.expire_cursors_below:
            raise CursorExpired(f"cursor {cursor} expired")
        if self.fail_after_pages is not None and self.pages_served >= self.fail_after_pages:
            self.fail_after_pages = None
            raise FakeConnectorFailure("injected failure mid-sync")
        visible = self.visible(now)
        start = int(page_token) if page_token else start_cursor
        chunk = visible[start : start + self.page_size]
        end = start + len(chunk)
        self.pages_served += 1
        deleted, self.pending_deletions = tuple(self.pending_deletions), []
        return SyncBatch(
            items=tuple(chunk),
            deleted_external_ids=deleted,
            next_page_token=str(end) if end < len(visible) else None,
            high_water_cursor=str(len(visible)),
        )


class FakeMailConnector:
    def __init__(self, feed: FakeFeed[NormalizedMessage]) -> None:
        self.feed = feed

    async def list_messages(
        self, *, cursor: str | None, page_token: str | None, now: datetime.datetime
    ) -> SyncBatch[NormalizedMessage]:
        return self.feed.page(cursor=cursor, page_token=page_token, now=now)


class FakeCalendarConnector:
    def __init__(self, feed: FakeFeed[NormalizedEvent]) -> None:
        self.feed = feed

    async def list_events(
        self, *, cursor: str | None, page_token: str | None, now: datetime.datetime
    ) -> SyncBatch[NormalizedEvent]:
        return self.feed.page(cursor=cursor, page_token=page_token, now=now)


class FakeAccounts:
    """Feeds per account email; registered as the ``fake`` provider factories."""

    def __init__(self) -> None:
        self.mail: dict[str, FakeFeed[NormalizedMessage]] = {}
        self.calendar: dict[str, FakeFeed[NormalizedEvent]] = {}

    def mail_feed(self, account_email: str, *, page_size: int = 20) -> FakeFeed[NormalizedMessage]:
        return self.mail.setdefault(account_email.lower(), FakeFeed(page_size=page_size))

    def calendar_feed(self, account_email: str, *, page_size: int = 20) -> FakeFeed[NormalizedEvent]:
        return self.calendar.setdefault(account_email.lower(), FakeFeed(page_size=page_size))

    def mail_connector(self, info: ConnectionInfo) -> FakeMailConnector:
        return FakeMailConnector(self.mail_feed(info.account_email))

    def calendar_connector(self, info: ConnectionInfo) -> FakeCalendarConnector:
        return FakeCalendarConnector(self.calendar_feed(info.account_email))


def _person(value: str | None) -> NormalizedPerson | None:
    pairs = getaddresses([value]) if value else []
    if not pairs or not pairs[0][1]:
        return None
    name, addr = pairs[0]
    return NormalizedPerson(email=addr.strip(), display_name=name.strip() or None)


def _people(value: str | None) -> tuple[NormalizedPerson, ...]:
    if not value:
        return ()
    return tuple(
        NormalizedPerson(email=a.strip(), display_name=n.strip() or None)
        for n, a in getaddresses([value])
        if a
    )


def _ids(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").replace("\n", " ").split() if part.strip()]


def parse_eml(raw: bytes, *, external_id: str, account_email: str) -> NormalizedMessage:
    """Map one RFC 5322 message to a ``NormalizedMessage`` (adapter logic, like a provider's)."""
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    assert isinstance(msg, EmailMessage)
    sender = _person(str(msg["From"] or ""))
    if sender is None:
        raise ValueError(f"{external_id}: no From address")
    plain = msg.get_body(preferencelist=("plain",))
    html = msg.get_body(preferencelist=("html",))
    rfc822 = (str(msg["Message-ID"]).strip() or None) if msg["Message-ID"] else None
    in_reply_to = (str(msg["In-Reply-To"]).strip() or None) if msg["In-Reply-To"] else None
    references = _ids(str(msg["References"])) if msg["References"] else []
    thread = references[0] if references else (in_reply_to or rfc822 or external_id)
    headers = {h: str(msg[h]) for h in HEADERS_KEPT if msg[h] is not None}
    categories = ["sent"] if sender.email.lower() == account_email.lower() else ["inbox"]
    if "List-Unsubscribe" in headers or headers.get("Precedence", "").lower() in {"bulk", "list"}:
        categories.append("promotions")
    if headers.get("Auto-Submitted", "no").lower() != "no" and "Auto-Submitted" in headers:
        categories.append("updates")
    return NormalizedMessage(
        external_id=external_id,
        thread_external_id=thread,
        rfc822_id=rfc822,
        in_reply_to=in_reply_to,
        sent_at=parsedate_to_datetime(str(msg["Date"])),
        sender=sender,
        to=_people(str(msg["To"]) if msg["To"] else None),
        cc=_people(str(msg["Cc"]) if msg["Cc"] else None),
        subject=str(msg["Subject"]) if msg["Subject"] else None,
        body_text=plain.get_content() if plain is not None else None,
        body_html=html.get_content() if html is not None and html is not plain else None,
        categories=tuple(categories),
        headers_subset=headers,
    )


def load_eml_dir(directory: Path, *, account_email: str) -> list[NormalizedMessage]:
    """Every ``*.eml`` in ``directory`` (external ID = file stem), in sent order."""
    messages = [
        parse_eml(p.read_bytes(), external_id=p.stem, account_email=account_email)
        for p in sorted(directory.glob("*.eml"))
    ]
    return sorted(messages, key=lambda m: (m.sent_at, m.external_id))


def feed_of(messages: Sequence[NormalizedMessage], *, page_size: int = 20) -> FakeFeed[NormalizedMessage]:
    feed: FakeFeed[NormalizedMessage] = FakeFeed(page_size=page_size)
    for m in messages:
        feed.add(m, delivered_at=m.sent_at)
    return feed
