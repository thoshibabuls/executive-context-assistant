"""Gmail adapter (slice 1.5, BACKEND_DESIGN.md §11.2, §11.6, §20).

- First run (no cursor): read the profile ``historyId`` **before** importing, so mail arriving
  during the import is caught incrementally; the import lists ``newer_than:30d`` (newest first)
  and its page token embeds that ``historyId`` so every page of one import returns the same
  high-water cursor.
- Incremental: ``history.list(startHistoryId)`` over all pages for added and deleted messages;
  ``messages.get(format=raw)`` for added IDs. HTTP 404 on an old ``historyId`` → ``CursorExpired``
  (sync resets the cursor and re-imports a bounded window with idempotent upserts).
- Quota: an in-job token bucket of 4,500 units/min per connection (``messages.get`` 20 units,
  ``messages.list`` 5, ``history.list`` 2); 429 and 5xx raise ``RateLimited`` /
  ``UpstreamUnavailable`` with ``Retry-After`` for the job's backoff.
- Labels map to neutral categories; provider IDs stay in ``external_id``/thread ID only.
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from eca.connectors.dto import NormalizedMessage, SyncBatch
from eca.connectors.fake import parse_eml
from eca.platform.errors import AuthRevoked, CursorExpired, RateLimited, UpstreamUnavailable

API = "https://gmail.googleapis.com/gmail/v1/users/me"
IMPORT_QUERY = "newer_than:30d"
UNITS_PER_MINUTE = 4500
COST = {"get": 20, "list": 5, "history": 2, "profile": 1}
LABEL_CATEGORIES = {
    "INBOX": "inbox",
    "SENT": "sent",
    "CATEGORY_PROMOTIONS": "promotions",
    "CATEGORY_SOCIAL": "social",
    "CATEGORY_UPDATES": "updates",
    "CATEGORY_FORUMS": "updates",
    "TRASH": "trash",
    "SPAM": "spam",
}
PAGE_SIZE = 50
TokenProvider = Callable[[], Awaitable[str]]


class QuotaBucket:
    """Token bucket per connection (one sync job per connection runs, so local is enough)."""

    def __init__(self, units_per_minute: int = UNITS_PER_MINUTE) -> None:
        self.capacity = units_per_minute
        self.tokens = float(units_per_minute)
        self.rate = units_per_minute / 60.0
        self.updated = time.monotonic()

    async def take(self, units: int) -> None:
        while True:
            now = time.monotonic()
            self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
            self.updated = now
            if self.tokens >= units:
                self.tokens -= units
                return
            await asyncio.sleep((units - self.tokens) / self.rate)


def retry_after_s(resp: httpx.Response) -> int | None:
    """The provider's ``Retry-After`` in seconds (delta form), for the job's backoff."""
    value = resp.headers.get("Retry-After", "").strip()
    return int(value) if value.isdigit() else None


def categories_from_labels(labels: list[str]) -> tuple[str, ...]:
    seen: list[str] = []
    for label in labels:
        cat = LABEL_CATEGORIES.get(label)
        if cat and cat not in seen:
            seen.append(cat)
    return tuple(seen) or ("inbox",)


class GmailConnector:
    def __init__(
        self,
        http: httpx.AsyncClient,
        token_provider: TokenProvider,
        *,
        account_email: str,
        bucket: QuotaBucket | None = None,
    ) -> None:
        self._http = http
        self._token = token_provider
        self._account = account_email
        self._bucket = bucket or QuotaBucket()

    async def _get(self, path: str, params: dict[str, Any], *, cost: str) -> dict[str, Any]:
        await self._bucket.take(COST[cost])
        token = await self._token()
        try:
            resp = await self._http.get(
                f"{API}{path}", params=params, headers={"Authorization": f"Bearer {token}"}, timeout=30
            )
        except httpx.HTTPError as exc:
            raise UpstreamUnavailable("Gmail API unreachable") from exc
        if resp.status_code == 401:
            raise AuthRevoked("Gmail rejected the access token")
        if resp.status_code == 404:
            raise (
                CursorExpired("Gmail history ID expired")
                if cost == "history"
                else UpstreamUnavailable("not found")
            )
        if resp.status_code == 429 or (resp.status_code == 403 and "rateLimit" in resp.text):
            raise RateLimited("Gmail rate limit", retry_after_s=retry_after_s(resp))
        if resp.status_code >= 500:
            raise UpstreamUnavailable(f"Gmail returned {resp.status_code}")
        if resp.status_code != 200:
            raise UpstreamUnavailable(f"Gmail returned {resp.status_code}")
        body: dict[str, Any] = resp.json()
        return body

    async def _message(self, message_id: str) -> NormalizedMessage | None:
        try:
            body = await self._get(f"/messages/{message_id}", {"format": "raw"}, cost="get")
        except UpstreamUnavailable:
            return None  # deleted between listing and fetching
        raw = base64.urlsafe_b64decode(body["raw"] + "=" * (-len(body["raw"]) % 4))
        parsed = parse_eml(raw, external_id=body["id"], account_email=self._account)
        return NormalizedMessage(
            external_id=body["id"],
            thread_external_id=body.get("threadId") or parsed.thread_external_id,
            rfc822_id=parsed.rfc822_id,
            in_reply_to=parsed.in_reply_to,
            sent_at=parsed.sent_at
            if parsed.sent_at
            else datetime.datetime.fromtimestamp(int(body["internalDate"]) / 1000, datetime.UTC),
            sender=parsed.sender,
            to=parsed.to,
            cc=parsed.cc,
            subject=parsed.subject,
            body_text=parsed.body_text,
            body_html=parsed.body_html,
            categories=categories_from_labels(list(body.get("labelIds", []))),
            headers_subset=parsed.headers_subset,
            provider_version=str(body.get("historyId")) if body.get("historyId") else None,
            deep_link=f"https://mail.google.com/mail/u/0/#all/{body['id']}",
        )

    async def list_messages(
        self, *, cursor: str | None, page_token: str | None, now: datetime.datetime
    ) -> SyncBatch[NormalizedMessage]:
        if cursor is None:
            return await self._import_page(page_token)
        return await self._history_page(cursor, page_token)

    async def _import_page(self, page_token: str | None) -> SyncBatch[NormalizedMessage]:
        if page_token:
            history_id, _, gmail_token = page_token.partition("|")
        else:
            profile = await self._get("/profile", {}, cost="profile")
            history_id, gmail_token = str(profile["historyId"]), ""
        params: dict[str, Any] = {"q": IMPORT_QUERY, "maxResults": PAGE_SIZE, "includeSpamTrash": "false"}
        if gmail_token:
            params["pageToken"] = gmail_token
        listing = await self._get("/messages", params, cost="list")
        items = [m for m in [await self._message(ref["id"]) for ref in listing.get("messages", [])] if m]
        nxt = listing.get("nextPageToken")
        return SyncBatch(
            items=tuple(items),
            deleted_external_ids=(),
            next_page_token=f"{history_id}|{nxt}" if nxt else None,
            high_water_cursor=history_id,
        )

    async def _history_page(self, cursor: str, page_token: str | None) -> SyncBatch[NormalizedMessage]:
        params: dict[str, Any] = {
            "startHistoryId": cursor,
            "historyTypes": ["messageAdded", "messageDeleted", "labelAdded", "labelRemoved"],
            "maxResults": 500,
        }
        if page_token:
            params["pageToken"] = page_token
        body = await self._get("/history", params, cost="history")
        added: list[str] = []
        deleted: list[str] = []
        for record in body.get("history", []):
            for entry in record.get("messagesAdded", []):
                if entry["message"]["id"] not in added:
                    added.append(entry["message"]["id"])
            for entry in record.get("messagesDeleted", []):
                deleted.append(entry["message"]["id"])
        items = [m for m in [await self._message(mid) for mid in added if mid not in deleted] if m]
        return SyncBatch(
            items=tuple(items),
            deleted_external_ids=tuple(deleted),
            next_page_token=body.get("nextPageToken"),
            high_water_cursor=str(body.get("historyId", cursor)),
        )
