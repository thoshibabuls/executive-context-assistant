"""Slice 1.7 Work API (IMPLEMENTATION_PLAN.md §3, BACKEND_DESIGN.md §16): keyset cursors, ``ETag`` /
``If-Match`` (412), ``base_version`` field-level 409, 422 validation, ``Idempotency-Key`` replay,
commands, merge and delete of user items, and 404 on another user's IDs (RLS). Also the
corrections recorded in ``feedback_events``. Synthetic data only.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.api.support import ApiHarness, ApiUser, api_harness
from tests.conftest import TempDatabase

pytestmark = pytest.mark.db


@pytest.fixture
def h(isolated_db: TempDatabase, tmp_path: Path) -> Iterator[ApiHarness]:
    with api_harness(isolated_db, tmp_path) as harness:
        yield harness


def _create(h: ApiHarness, user: ApiUser, title: str, **extra: Any) -> dict[str, Any]:
    r = h.request(user, "POST", "/api/v1/work-items", json={"title": title, **extra})
    assert r.status_code == 201, r.text
    return dict(r.json())


def test_create_with_idempotency_key_replays_the_same_item(h: ApiHarness) -> None:
    avery = h.user("avery@brightwater.example", "Avery Lindqvist")
    body = {"title": "Draft the hiring plan", "due_at": "2026-10-09T17:00:00+00:00"}
    first = h.request(avery, "POST", "/api/v1/work-items", json=body, headers={"Idempotency-Key": "create-1"})
    assert (
        first.status_code == 201 and first.headers["Location"] == f"/api/v1/work-items/{first.json()['id']}"
    )
    item = first.json()
    assert (item["origin"], item["verification_status"], item["direction"]) == (
        "user",
        "user_created",
        "my_task",
    )
    again = h.request(avery, "POST", "/api/v1/work-items", json=body, headers={"Idempotency-Key": "create-1"})
    assert again.status_code == 201 and again.json()["id"] == item["id"]
    assert again.headers.get("Idempotent-Replay") == "true"
    other_body = h.request(
        avery,
        "POST",
        "/api/v1/work-items",
        json={"title": "Different"},
        headers={"Idempotency-Key": "create-1"},
    )
    assert other_body.status_code == 422  # the key was used for another request
    assert h.scalar("SELECT count(*) FROM work_items") == 1


def test_validation_errors_are_422_problem_details(h: ApiHarness) -> None:
    avery = h.user("avery@brightwater.example", "Avery Lindqvist")
    for body in ({"title": ""}, {"title": "x", "direction": "sideways"}, {"title": "x", "unknown": 1}):
        r = h.request(avery, "POST", "/api/v1/work-items", json=body)
        assert r.status_code == 422, body
        assert r.headers["content-type"].startswith("application/problem+json")
        assert r.json()["code"] == "validation_failed" and r.json()["errors"]
    item = _create(h, avery, "Valid")
    assert h.request(avery, "PATCH", f"/api/v1/work-items/{item['id']}", json={}).status_code == 422


def test_if_match_and_base_version_conflicts(h: ApiHarness) -> None:
    avery = h.user("avery@brightwater.example", "Avery Lindqvist")
    item = _create(h, avery, "Prepare the board pack")
    url = f"/api/v1/work-items/{item['id']}"
    got = h.request(avery, "GET", url)
    etag = got.headers["ETag"]
    ok = h.request(
        avery, "PATCH", url, json={"title": "Prepare the Q4 board pack"}, headers={"If-Match": etag}
    )
    assert ok.status_code == 200 and ok.headers["ETag"] != etag
    stale = h.request(avery, "PATCH", url, json={"notes": "late edit"}, headers={"If-Match": etag})
    assert stale.status_code == 412 and stale.json()["code"] == "precondition_failed"

    version_before = got.json()["version"]
    # base_version: a change to another field merges; a change to the same field is a 409.
    merged = h.request(avery, "PATCH", url, json={"notes": "agenda attached", "base_version": version_before})
    assert merged.status_code == 200
    clash = h.request(avery, "PATCH", url, json={"title": "Prepare the pack", "base_version": version_before})
    assert clash.status_code == 409
    assert [e["field"] for e in clash.json()["errors"]] == ["title"]
    assert clash.json()["current_version"] == merged.json()["version"]
    assert h.request(avery, "GET", url).json()["title"] == "Prepare the Q4 board pack"


def test_commands_merge_delete_and_feedback(h: ApiHarness) -> None:
    avery = h.user("avery@brightwater.example", "Avery Lindqvist")
    a = _create(h, avery, "Send the revised budget")
    b = _create(h, avery, "Send revised budget")
    for command, status in (
        ("start", "in_progress"),
        ("complete", "done"),
        ("reopen", "open"),
        ("cancel", "cancelled"),
    ):
        r = h.request(avery, "POST", f"/api/v1/work-items/{a['id']}/{command}")
        assert r.status_code == 200, (command, r.text)
        assert r.json()["lifecycle_status"] == status, command
    assert h.request(avery, "POST", f"/api/v1/work-items/{a['id']}/explode").status_code == 422
    merged = h.request(avery, "POST", f"/api/v1/work-items/{b['id']}/merge", json={"into_id": a["id"]})
    assert merged.status_code == 200 and merged.json()["id"] == a["id"]
    assert h.request(avery, "GET", f"/api/v1/work-items/{b['id']}").status_code in (200, 404)
    c = _create(h, avery, "Throwaway")
    assert h.request(avery, "DELETE", f"/api/v1/work-items/{c['id']}").status_code == 204
    assert h.request(avery, "GET", f"/api/v1/work-items/{c['id']}").status_code == 404
    edit = h.request(
        avery, "PATCH", f"/api/v1/work-items/{a['id']}", json={"due_at": "2026-10-10T09:00:00+00:00"}
    )
    assert edit.status_code == 200
    actions = {r[0] for r in h.rows("SELECT action FROM feedback_events")}
    assert actions, "corrections are recorded as feedback events"
    timeline = h.request(avery, "GET", f"/api/v1/work-items/{a['id']}").json()["timeline"]
    assert timeline and all("event_type" in e for e in timeline)


def test_keyset_cursor_pagination(h: ApiHarness) -> None:
    avery = h.user("avery@brightwater.example", "Avery Lindqvist")
    titles = [f"Task {i:02d}" for i in range(7)]
    for i, t in enumerate(titles):
        _create(h, avery, t, due_at=f"2026-10-{10 + i:02d}T09:00:00+00:00")
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        params = {"limit": 3, "sort": "due"} | ({"cursor": cursor} if cursor else {})
        r = h.request(avery, "GET", "/api/v1/work-items", params=params)
        assert r.status_code == 200
        body = r.json()
        seen += [i["title"] for i in body["items"]]
        pages += 1
        cursor = body.get("next_cursor")
        if not cursor:
            break
    assert seen == titles and pages == 3
    # A cursor is bound to its list and user.
    first = h.request(avery, "GET", "/api/v1/work-items", params={"limit": 3}).json()["next_cursor"]
    assert (
        h.request(
            avery, "GET", "/api/v1/work-items", params={"limit": 3, "sort": "created", "cursor": first}
        ).status_code
        == 422
    )
    assert (
        h.request(avery, "GET", "/api/v1/work-items", params={"cursor": first[:-2] + "xx"}).status_code == 422
    )
    blake = h.user("blake@tallgrass.example", "Blake Ortiz")
    assert h.request(blake, "GET", "/api/v1/work-items", params={"cursor": first}).status_code == 422
    assert h.request(avery, "GET", "/api/v1/work-items", params={"limit": 0}).status_code == 422


def test_another_users_ids_are_404(h: ApiHarness) -> None:
    avery = h.user("avery@brightwater.example", "Avery Lindqvist")
    blake = h.user("blake@tallgrass.example", "Blake Ortiz")
    item = _create(h, avery, "Private task")
    url = f"/api/v1/work-items/{item['id']}"
    assert h.request(blake, "GET", url).status_code == 404
    assert h.request(blake, "PATCH", url, json={"title": "stolen"}).status_code == 404
    assert h.request(blake, "POST", f"{url}/complete").status_code == 404
    assert h.request(blake, "DELETE", url).status_code == 404
    mine = _create(h, blake, "Blake's task")
    assert (
        h.request(
            blake, "POST", f"/api/v1/work-items/{mine['id']}/merge", json={"into_id": item["id"]}
        ).status_code
        == 404
    )
    assert h.request(blake, "GET", "/api/v1/work-items").json()["items"][0]["title"] == "Blake's task"
    assert h.request(avery, "GET", url).json()["title"] == "Private task"
    for path in (
        f"/api/v1/people/{avery.self_person_id}",
        f"/api/v1/decisions/{item['id']}",
        f"/api/v1/evidence/{item['id']}",
    ):
        assert h.request(blake, "GET", path).status_code == 404, path


def test_today_and_data_summary(h: ApiHarness) -> None:
    avery = h.user("avery@brightwater.example", "Avery Lindqvist")
    _create(h, avery, "Due soon", due_at="2026-10-04T09:00:00+00:00")
    today = h.request(avery, "GET", "/api/v1/today")
    assert today.status_code == 200 and isinstance(today.json(), dict)
    summary = h.request(avery, "GET", "/api/v1/data-summary")
    assert summary.status_code == 200
    assert "Due soon" not in summary.text  # counts and categories only, no content
