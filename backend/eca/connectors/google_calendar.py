"""Google Calendar adapter (slice 1.6, BACKEND_DESIGN.md §11.3).

- Full sync: ``events.list(singleEvents=true, timeMin=now-30d, timeMax=now+60d)``; the final page's
  ``nextSyncToken`` becomes the cursor together with the ``query_fingerprint`` of the parameters
  it is bound to (``"<fingerprint>|<syncToken>"``). Page tokens carry the window so every page of
  one run uses identical parameters.
- Incremental: ``events.list(syncToken=…)`` with the same parameters (includes cancelled events).
- HTTP 410 Gone → ``CursorExpired`` (sync clears the cursor and runs a full window sync).
- The daily window roll-forward is done by ingestion (a full sync replaces the cursor).
- In-job limit 300 requests/min.
"""

from __future__ import annotations

import datetime
import hashlib
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from eca.connectors.dto import NormalizedAttendee, NormalizedEvent, NormalizedPerson, SyncBatch
from eca.connectors.gmail import QuotaBucket
from eca.platform.errors import AuthRevoked, CursorExpired, RateLimited, UpstreamUnavailable

API = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
PAST = datetime.timedelta(days=30)
FUTURE = datetime.timedelta(days=60)
TokenProvider = Callable[[], Awaitable[str]]


def query_fingerprint(time_min: str, time_max: str) -> str:
    return hashlib.sha256(f"singleEvents=true|{time_min}|{time_max}".encode()).hexdigest()[:16]


def _dt(value: dict[str, Any] | None) -> tuple[datetime.datetime | None, str | None]:
    if not value:
        return None, None
    if "dateTime" in value:
        return datetime.datetime.fromisoformat(value["dateTime"].replace("Z", "+00:00")), value.get(
            "timeZone"
        )
    if "date" in value:  # all-day event
        day = datetime.date.fromisoformat(value["date"])
        return datetime.datetime.combine(day, datetime.time(0, 0), tzinfo=datetime.UTC), value.get("timeZone")
    return None, None


def normalize_event(item: dict[str, Any]) -> NormalizedEvent | None:
    start, tz = _dt(item.get("start"))
    end, _ = _dt(item.get("end"))
    if start is None:
        if item.get("status") != "cancelled":
            return None
        start = end = datetime.datetime(1970, 1, 1, tzinfo=datetime.UTC)  # cancelled instance tombstone
    organizer = item.get("organizer") or {}
    conference = next(
        (e.get("uri") for e in (item.get("conferenceData") or {}).get("entryPoints", []) if e.get("uri")),
        None,
    )
    return NormalizedEvent(
        external_id=item["id"],
        series_external_id=item.get("recurringEventId"),
        ical_uid=item.get("iCalUID"),
        start=start,
        end=end or start,
        timezone=tz,
        title=item.get("summary"),
        description=item.get("description"),
        attendees=tuple(
            NormalizedAttendee(a["email"], a.get("displayName"), a.get("responseStatus"))
            for a in item.get("attendees", [])
            if a.get("email")
        ),
        organizer=NormalizedPerson(organizer["email"], organizer.get("displayName"))
        if organizer.get("email")
        else None,
        conference_uri=conference or item.get("hangoutLink"),
        status=item.get("status", "confirmed"),
        provider_version=item.get("etag"),
    )


class GoogleCalendarConnector:
    def __init__(
        self, http: httpx.AsyncClient, token_provider: TokenProvider, *, bucket: QuotaBucket | None = None
    ):
        self._http = http
        self._token = token_provider
        self._bucket = bucket or QuotaBucket(units_per_minute=300)

    async def _list(self, params: dict[str, Any]) -> dict[str, Any]:
        await self._bucket.take(1)
        token = await self._token()
        try:
            resp = await self._http.get(
                API, params=params, headers={"Authorization": f"Bearer {token}"}, timeout=30
            )
        except httpx.HTTPError as exc:
            raise UpstreamUnavailable("Calendar API unreachable") from exc
        if resp.status_code == 410:
            raise CursorExpired("calendar sync token expired")
        if resp.status_code == 401:
            raise AuthRevoked("Calendar rejected the access token")
        if resp.status_code == 429 or (resp.status_code == 403 and "usageLimits" in resp.text):
            raise RateLimited("Calendar rate limit", details={"retry_after": resp.headers.get("Retry-After")})
        if resp.status_code != 200:
            raise UpstreamUnavailable(f"Calendar returned {resp.status_code}")
        body: dict[str, Any] = resp.json()
        return body

    async def list_events(
        self, *, cursor: str | None, page_token: str | None, now: datetime.datetime
    ) -> SyncBatch[NormalizedEvent]:
        if cursor is not None and not page_token:
            fingerprint, _, sync_token = cursor.partition("|")
            return await self._page({"syncToken": sync_token}, fingerprint, None)
        if page_token and page_token.startswith("inc|"):
            _, fingerprint, sync_token, gtoken = page_token.split("|", 3)
            return await self._page({"syncToken": sync_token}, fingerprint, gtoken, incremental=sync_token)
        if page_token:
            _, time_min, time_max, gtoken = page_token.split("|", 3)
        else:
            time_min = (now - PAST).astimezone(datetime.UTC).isoformat().replace("+00:00", "Z")
            time_max = (now + FUTURE).astimezone(datetime.UTC).isoformat().replace("+00:00", "Z")
            gtoken = ""
        params = {"singleEvents": "true", "timeMin": time_min, "timeMax": time_max, "maxResults": 250}
        fingerprint = query_fingerprint(time_min, time_max)
        return await self._page(params, fingerprint, gtoken or None, window=(time_min, time_max))

    async def _page(
        self,
        params: dict[str, Any],
        fingerprint: str,
        gtoken: str | None,
        *,
        window: tuple[str, str] | None = None,
        incremental: str | None = None,
    ) -> SyncBatch[NormalizedEvent]:
        if gtoken:
            params = {**params, "pageToken": gtoken}
        body = await self._list(params)
        items = tuple(e for e in (normalize_event(i) for i in body.get("items", [])) if e is not None)
        nxt = body.get("nextPageToken")
        if nxt:
            token = (
                f"full|{window[0]}|{window[1]}|{nxt}"
                if window
                else f"inc|{fingerprint}|{incremental or params.get('syncToken')}|{nxt}"
            )
            return SyncBatch(
                items=items, deleted_external_ids=(), next_page_token=token, high_water_cursor=""
            )
        return SyncBatch(
            items=items,
            deleted_external_ids=(),
            next_page_token=None,
            high_water_cursor=f"{fingerprint}|{body.get('nextSyncToken', '')}",
        )
