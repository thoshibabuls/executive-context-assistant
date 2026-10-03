"""Contract tests of the Gmail and Google Calendar adapters (slices 1.5, 1.6; BACKEND_DESIGN.md §11,
§20) against synthetic recorded responses served by ``httpx.MockTransport``.

Gmail: the profile ``historyId`` is read before the 30-day import and pinned in every page token;
raw messages are parsed and labels mapped to neutral categories; a message deleted between list
and get is skipped; ``history.list`` pages added and deleted IDs; 404 on an old ``historyId`` is
``CursorExpired``; 401 is ``AuthRevoked``; 429 and quota 403 are ``RateLimited`` with the
``Retry-After``; 5xx and transport errors are ``UpstreamUnavailable``.
Calendar: the full sync window with ``singleEvents``, page tokens that carry the window, the final
``nextSyncToken`` as ``fingerprint|token``, incremental requests that repeat ``singleEvents``, 410
as ``CursorExpired``, cancelled instances, all-day events, attendees and conference links.
No request leaves the process; every value is synthetic.
"""

from __future__ import annotations

import base64
import datetime
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from eca.connectors import GmailConnector, GoogleCalendarConnector
from eca.connectors.google_calendar import query_fingerprint
from eca.platform.errors import AuthRevoked, CursorExpired, RateLimited, UpstreamUnavailable

NOW = datetime.datetime(2026, 10, 3, 12, 0, tzinfo=datetime.UTC)
ACCOUNT = "avery@brightwater.example"


def _raw(n: int, subject: str = "Hello") -> str:
    eml = (
        f"Message-ID: <m{n}@test.example>\r\n"
        "From: Jordan Blake <jordan.blake@kestrel.example>\r\n"
        f"To: Avery Lindqvist <{ACCOUNT}>\r\n"
        f"Subject: {subject} {n}\r\n"
        "Date: Mon, 21 Sep 2026 09:00:00 +0000\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n\r\n"
        f"Synthetic body {n}.\r\n"
    )
    return base64.urlsafe_b64encode(eml.encode()).decode().rstrip("=")


class Recorder:
    """Serves canned responses by (method, path) and records every request."""

    def __init__(self, routes: dict[str, Callable[[httpx.Request], httpx.Response]]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = urlparse(str(request.url)).path
        for prefix, route in self.routes.items():
            if path.endswith(prefix):
                return route(request)
        return httpx.Response(404)

    def params(self, n: int) -> dict[str, list[str]]:
        return parse_qs(urlparse(str(self.requests[n].url)).query)


async def _token() -> str:
    return "at-test"


def _gmail(routes: dict[str, Callable[[httpx.Request], httpx.Response]]) -> tuple[GmailConnector, Recorder]:
    rec = Recorder(routes)
    return GmailConnector(
        httpx.AsyncClient(transport=httpx.MockTransport(rec)), _token, account_email=ACCOUNT
    ), rec


def _message(n: int, labels: list[str]) -> Callable[[httpx.Request], httpx.Response]:
    return lambda r: httpx.Response(
        200,
        json={
            "id": f"m{n}",
            "threadId": f"t{n}",
            "labelIds": labels,
            "raw": _raw(n),
            "historyId": "900",
            "internalDate": "0",
        },
    )


async def test_gmail_first_import_pins_the_profile_history_id() -> None:
    pages = iter(
        [
            {"messages": [{"id": "m1"}, {"id": "m2"}], "nextPageToken": "p2"},
            {"messages": [{"id": "m3"}]},
        ]
    )
    gmail, rec = _gmail(
        {
            "/profile": lambda r: httpx.Response(200, json={"historyId": "777", "emailAddress": ACCOUNT}),
            "/messages": lambda r: httpx.Response(200, json=next(pages)),
            "/messages/m1": _message(1, ["INBOX", "CATEGORY_UPDATES"]),
            "/messages/m2": _message(2, ["SENT"]),
            "/messages/m3": lambda r: httpx.Response(404),  # deleted between list and get
        }
    )
    first = await gmail.list_messages(cursor=None, page_token=None, now=NOW)
    assert [m.external_id for m in first.items] == ["m1", "m2"]
    assert first.high_water_cursor == "777" and first.next_page_token == "777|p2"
    assert first.items[0].categories == ("inbox", "updates") and first.items[1].categories == ("sent",)
    assert first.items[0].thread_external_id == "t1" and first.items[0].subject == "Hello 1"
    assert first.items[0].sender.email == "jordan.blake@kestrel.example"
    assert rec.params(1)["q"] == ["newer_than:30d"]
    second = await gmail.list_messages(cursor=None, page_token=first.next_page_token, now=NOW)
    assert second.items == () and second.next_page_token is None and second.high_water_cursor == "777"
    assert [urlparse(str(r.url)).path.rsplit("/", 1)[-1] for r in rec.requests].count("profile") == 1


async def test_gmail_history_pages_added_and_deleted_messages() -> None:
    gmail, rec = _gmail(
        {
            "/history": lambda r: httpx.Response(
                200,
                json={
                    "history": [
                        {"messagesAdded": [{"message": {"id": "m5"}}, {"message": {"id": "m6"}}]},
                        {"messagesDeleted": [{"message": {"id": "m6"}}, {"message": {"id": "m1"}}]},
                    ],
                    "historyId": "805",
                    "nextPageToken": "h2",
                },
            ),
            "/messages/m5": _message(5, ["INBOX", "TRASH"]),
        }
    )
    batch = await gmail.list_messages(cursor="800", page_token=None, now=NOW)
    assert [m.external_id for m in batch.items] == ["m5"]  # m6 was added then deleted: not fetched
    assert batch.items[0].categories == ("inbox", "trash")
    assert batch.deleted_external_ids == ("m6", "m1")
    assert batch.next_page_token == "h2" and batch.high_water_cursor == "805"
    assert rec.params(0)["startHistoryId"] == ["800"]


@pytest.mark.parametrize(
    ("status", "body", "headers", "error", "retry"),
    [
        (404, "", {}, CursorExpired, None),
        (401, "", {}, AuthRevoked, None),
        (429, "", {"Retry-After": "42"}, RateLimited, 42),
        (403, '{"error": {"errors": [{"reason": "rateLimitExceeded"}]}}', {}, RateLimited, None),
        (500, "", {}, UpstreamUnavailable, None),
        (400, "", {}, UpstreamUnavailable, None),
    ],
)
async def test_gmail_errors(
    status: int, body: str, headers: dict[str, str], error: type[Exception], retry: int | None
) -> None:
    gmail, _ = _gmail({"/history": lambda r: httpx.Response(status, text=body, headers=headers)})
    with pytest.raises(error) as caught:
        await gmail.list_messages(cursor="800", page_token=None, now=NOW)
    if isinstance(caught.value, RateLimited):
        assert caught.value.retry_after_s == retry


async def test_gmail_transport_error_is_upstream_unavailable() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("synthetic")

    gmail, _ = _gmail({"/history": boom})
    with pytest.raises(UpstreamUnavailable):
        await gmail.list_messages(cursor="800", page_token=None, now=NOW)


# ---------------------------------------------------------------- calendar


def _event(n: int, **extra: Any) -> dict[str, Any]:
    return {
        "id": f"e{n}",
        "etag": f'"etag-{n}"',
        "iCalUID": f"e{n}@test.example",
        "status": "confirmed",
        "summary": f"Meeting {n}",
        "start": {"dateTime": "2026-10-05T16:00:00Z", "timeZone": "America/Los_Angeles"},
        "end": {"dateTime": "2026-10-05T16:30:00Z"},
        "attendees": [
            {"email": ACCOUNT, "displayName": "Avery Lindqvist", "responseStatus": "accepted"},
            {"email": "jordan.blake@kestrel.example", "responseStatus": "needsAction"},
            {"displayName": "Room without email"},
        ],
        "organizer": {"email": ACCOUNT},
        **extra,
    }


def _calendar(responses: list[httpx.Response]) -> tuple[GoogleCalendarConnector, Recorder]:
    it = iter(responses)
    rec = Recorder({"/events": lambda r: next(it)})
    return GoogleCalendarConnector(httpx.AsyncClient(transport=httpx.MockTransport(rec)), _token), rec


async def test_calendar_full_sync_window_pages_and_sync_token() -> None:
    cal, rec = _calendar(
        [
            httpx.Response(
                200,
                json={
                    "items": [
                        _event(1, conferenceData={"entryPoints": [{"uri": "https://meet.example/abc"}]})
                    ],
                    "nextPageToken": "g2",
                },
            ),
            httpx.Response(
                200,
                json={
                    "items": [
                        _event(2, start={"date": "2026-10-06"}, end={"date": "2026-10-07"}),
                        {"id": "e3_20261007", "status": "cancelled", "recurringEventId": "e3"},
                    ],
                    "nextSyncToken": "sync-1",
                },
            ),
        ]
    )
    first = await cal.list_events(cursor=None, page_token=None, now=NOW)
    p0 = rec.params(0)
    assert p0["singleEvents"] == ["true"]
    assert p0["timeMin"] == ["2026-09-03T12:00:00Z"] and p0["timeMax"] == ["2026-12-02T12:00:00Z"]
    e1 = first.items[0]
    assert (e1.title, e1.conference_uri, e1.timezone, e1.provider_version) == (
        "Meeting 1",
        "https://meet.example/abc",
        "America/Los_Angeles",
        '"etag-1"',
    )
    assert [a.email for a in e1.attendees] == [ACCOUNT, "jordan.blake@kestrel.example"]
    assert first.next_page_token is not None and first.high_water_cursor == ""
    second = await cal.list_events(cursor=None, page_token=first.next_page_token, now=NOW)
    p1 = rec.params(1)
    assert p1["timeMin"] == p0["timeMin"] and p1["pageToken"] == ["g2"]  # the window is carried
    all_day, cancelled = second.items
    assert all_day.start == datetime.datetime(2026, 10, 6, tzinfo=datetime.UTC)
    assert (cancelled.status, cancelled.series_external_id) == ("cancelled", "e3")
    fingerprint = query_fingerprint(p0["timeMin"][0], p0["timeMax"][0])
    assert second.high_water_cursor == f"{fingerprint}|sync-1" and second.next_page_token is None


async def test_calendar_incremental_sync_repeats_single_events() -> None:
    cal, rec = _calendar(
        [
            httpx.Response(200, json={"items": [_event(4)], "nextPageToken": "g2"}),
            httpx.Response(200, json={"items": [], "nextSyncToken": "sync-3"}),
        ]
    )
    first = await cal.list_events(cursor="fp|sync-2", page_token=None, now=NOW)
    p0 = rec.params(0)
    assert p0["syncToken"] == ["sync-2"] and p0["singleEvents"] == ["true"]
    assert "timeMin" not in p0 and "timeMax" not in p0  # not allowed with a sync token
    second = await cal.list_events(cursor="fp|sync-2", page_token=first.next_page_token, now=NOW)
    p1 = rec.params(1)
    assert p1["syncToken"] == ["sync-2"] and p1["pageToken"] == ["g2"] and p1["singleEvents"] == ["true"]
    assert second.high_water_cursor == "fp|sync-3"


@pytest.mark.parametrize(
    ("status", "headers", "error"),
    [
        (410, {}, CursorExpired),
        (401, {}, AuthRevoked),
        (429, {"Retry-After": "7"}, RateLimited),
        (503, {}, UpstreamUnavailable),
    ],
)
async def test_calendar_errors(status: int, headers: dict[str, str], error: type[Exception]) -> None:
    cal, _ = _calendar([httpx.Response(status, headers=headers)])
    with pytest.raises(error) as caught:
        await cal.list_events(cursor="fp|sync-2", page_token=None, now=NOW)
    if isinstance(caught.value, RateLimited):
        assert caught.value.retry_after_s == 7
