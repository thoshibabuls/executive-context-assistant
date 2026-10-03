"""Phase 4 API tests (IMPLEMENTATION_PLAN.md §0.8): uploads and the meeting routes.

Upload init with ``Idempotency-Key`` replay, sha256 dedupe, the 10/hour limit, 409s and 404 on
another user's IDs; then a synthetic WebVTT transcript through the real handlers (fake AI-10 and
AI-11 answers) to ``GET /meetings/{id}``, ``PUT /meetings/{id}/speakers``,
``GET /meetings/{id}/prep`` and ``POST /meetings/{id}/prep/asks``. All content is synthetic.
"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Any

import pytest

from eca.intelligence.provider.types import GenerateRequest
from tests.api.support import ApiHarness, ApiUser, api_harness
from tests.conftest import TempDatabase

pytestmark = pytest.mark.db

VTT = (
    b"WEBVTT\n\n"
    b"00:00:01.000 --> 00:00:05.000\n<v Avery>Thanks for joining, this is Avery.\n\n"
    b"00:00:06.000 --> 00:00:10.000\n<v Jordan>I will send the revised budget by Friday.\n\n"
    b"00:00:11.000 --> 00:00:15.000\n<v Avery>We decided to launch the pilot in November.\n\n"
    b"00:00:16.000 --> 00:00:20.000\n<v Jordan>Who owns the vendor contract?\n"
)
JORDAN = ("jordan.blake@kestrel.example", "Jordan Blake")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _init(h: ApiHarness, user: ApiUser, data: bytes, mime: str, key: str | None, **extra: Any) -> Any:
    headers = {"Idempotency-Key": key} if key else {}
    body = {"mime": mime, "bytes": len(data), "sha256": _sha(data), **extra}
    return h.request(user, "POST", "/api/v1/recordings", json=body, headers=headers)


def _upload(h: ApiHarness, user: ApiUser, data: bytes, mime: str = "text/vtt", **extra: Any) -> str:
    r = _init(h, user, data, mime, f"k-{uuid.uuid4()}", **extra)
    assert r.status_code == 201, r.text
    upload = r.json()["upload"]
    put = h.client.put(upload["url"], content=data, headers=upload["headers"])
    assert put.status_code == 201, put.text
    rec_id = r.json()["recording"]["id"]
    done = h.request(user, "POST", f"/api/v1/recordings/{rec_id}/complete")
    assert done.status_code == 202 and done.json()["status"] == "uploaded", done.text
    return str(rec_id)


@pytest.fixture
def h(isolated_db: TempDatabase, tmp_path: Path) -> Any:
    with api_harness(isolated_db, tmp_path) as harness:
        yield harness


def test_unauthenticated_and_missing_csrf_are_refused(h: ApiHarness) -> None:
    avery = h.user("avery@brightwater.example", "Avery Lindqvist")
    h.client.cookies.clear()
    assert h.client.get("/api/v1/recordings").status_code == 401
    for name, value in avery.cookies.items():
        h.client.cookies.set(name, value)
    r = h.client.post("/api/v1/recordings", json={}, headers={"Idempotency-Key": "x"})
    assert r.status_code == 403  # no CSRF header


def test_upload_init_replay_dedupe_conflicts_and_isolation(h: ApiHarness) -> None:
    avery = h.user("avery@brightwater.example", "Avery Lindqvist")
    other = h.user("riley@elsewhere.example", "Riley Moss")

    missing_key = _init(h, avery, VTT, "text/vtt", None)
    assert missing_key.status_code == 422

    first = _init(h, avery, VTT, "text/vtt", "key-1", title="Budget sync")
    assert first.status_code == 201
    body = first.json()
    rec_id = body["recording"]["id"]
    assert first.headers["Location"] == f"/api/v1/recordings/{rec_id}"
    assert body["duplicate"] is False and body["recording"]["status"] == "pending_upload"
    assert body["upload"]["method"] == "PUT" and body["upload"]["headers"] == {"Content-Type": "text/vtt"}

    replay = _init(h, avery, VTT, "text/vtt", "key-1", title="Budget sync")
    assert replay.status_code == 201 and replay.json() == body
    assert replay.headers.get("Idempotent-Replay") == "true"
    reused = _init(h, avery, b"WEBVTT\n\n00:01.000 --> 00:02.000\nother", "text/vtt", "key-1")
    assert reused.status_code == 422  # same key, different request

    # complete before the object exists: 409
    early = h.request(avery, "POST", f"/api/v1/recordings/{rec_id}/complete")
    assert early.status_code == 409 and early.json()["code"] == "upload_incomplete"

    url, headers = body["upload"]["url"], body["upload"]["headers"]
    assert h.client.put(url, content=VTT, headers={"Content-Type": "text/plain"}).status_code == 415
    assert h.client.put(url, content=VTT + b"extra", headers=headers).status_code == 413
    assert h.client.put(url, content=VTT, headers=headers).status_code == 201
    assert h.client.put(url[:-3] + "AAA", content=VTT, headers=headers).status_code == 403

    done = h.request(avery, "POST", f"/api/v1/recordings/{rec_id}/complete")
    assert done.status_code == 202 and done.json()["status"] == "uploaded"
    again = h.request(avery, "POST", f"/api/v1/recordings/{rec_id}/complete")
    assert again.status_code == 202 and again.json()["status"] == "uploaded"  # repeat: current state

    dup = _init(h, avery, VTT, "text/vtt", "key-2")
    assert dup.status_code == 200
    assert dup.json()["duplicate"] is True and dup.json()["upload"] is None
    assert dup.json()["recording"]["id"] == rec_id

    # Another user: 404 on every route that takes the recording ID; the same media is a new upload.
    for method, path in (
        ("GET", f"/api/v1/recordings/{rec_id}"),
        ("POST", f"/api/v1/recordings/{rec_id}/complete"),
        ("POST", f"/api/v1/recordings/{rec_id}/retry"),
    ):
        assert h.request(other, method, path).status_code == 404, path
    assert (
        h.request(other, "PUT", f"/api/v1/recordings/{rec_id}/meeting", json={"meeting_id": None}).status_code
        == 404
    )
    assert h.request(other, "GET", "/api/v1/recordings").json()["items"] == []
    theirs = _init(h, other, VTT, "text/vtt", "key-1")
    assert theirs.status_code == 201 and theirs.json()["recording"]["id"] != rec_id

    bad_type = _init(h, avery, b"%PDF-1.7", "application/pdf", "key-3")
    assert bad_type.status_code == 422


def test_upload_init_is_limited_to_10_per_hour(h: ApiHarness) -> None:
    user = h.user("casey@brightwater.example", "Casey Park")
    for i in range(10):
        r = _init(h, user, f"WEBVTT\n\n00:01.000 --> 00:02.000\nline {i}".encode(), "text/vtt", f"k{i}")
        assert r.status_code == 201, (i, r.text)
    over = _init(h, user, b"WEBVTT\n\n00:01.000 --> 00:02.000\nline 10", "text/vtt", "k10")
    assert over.status_code == 429
    assert int(over.headers["Retry-After"]) > 0


def _meeting_extraction(request: GenerateRequest) -> dict[str, Any]:
    return {
        "summary": "Budget follow-up and pilot timing.",
        "topics": ["budget", "pilot"],
        "concerns": [],
        "statements": [
            {
                "statement_kind": "promise",
                "action": "Send the revised budget",
                "speaker": "Jordan",
                "owner_ref": "speaker",
                "due_text": "by Friday",
                "confidence": 0.92,
                "evidence": {
                    "segment_seq": 1,
                    "start_ms": 6000,
                    "quote": "I will send the revised budget by Friday",
                },
            }
        ],
        "status_signals": [],
        "decisions": [
            {
                "kind": "decision",
                "statement": "Launch the pilot in November",
                "confidence": 0.9,
                "evidence": {"segment_seq": 2, "quote": "launch the pilot in November"},
            },
            {
                "kind": "open_question",
                "statement": "Who owns the vendor contract?",
                "confidence": 0.85,
                "evidence": {"segment_seq": 3, "quote": "Who owns the vendor contract?"},
            },
        ],
        "speaker_mapping": [
            {"label": "Jordan", "person_email": JORDAN[0], "confidence": 0.7, "quote": ""},
        ],
    }


def _asks(request: GenerateRequest) -> dict[str, Any]:
    return {
        "asks": [
            {"text": "Ask Jordan to confirm the budget date", "citations": ["S2"]},
            {"text": "Uncited ask", "citations": ["S99"]},
        ],
        "answerable": True,
    }


def test_transcript_upload_to_meeting_page_speakers_prep_and_asks(h: ApiHarness) -> None:
    avery = h.user("avery@brightwater.example", "Avery Lindqvist")
    other = h.user("riley@elsewhere.example", "Riley Moss")
    jordan_id = h.person(avery, *JORDAN)
    h.fake_ai.responders["MeetingExtraction"] = _meeting_extraction
    h.fake_ai.responders["MeetingAsks"] = _asks

    rec_id = _upload(h, avery, VTT, title="Budget sync", occurred_at="2026-09-21T09:00:00+00:00")
    h.drain()

    rec = h.request(avery, "GET", f"/api/v1/recordings/{rec_id}").json()
    assert rec["status"] == "ready", rec
    assert rec["transcription_model"] == "file:vtt"
    meeting_id = rec["meeting_id"]
    assert h.scalar("SELECT count(*) FROM transcript_segments") == 4
    assert len(h.fake_ai.calls_for("MeetingExtraction")) == 1
    assert h.fake_ai.calls_for("Transcription") == []  # transcript files never reach AI-09

    page = h.request(avery, "GET", f"/api/v1/meetings/{meeting_id}")
    assert page.status_code == 200 and page.headers["ETag"]
    body = page.json()
    assert body["meeting"]["processing_status"] == "ready"
    assert body["summary_status"] == "ready"
    assert body["summary"]["origin"] == "ai" and body["summary"]["verification_status"] == "suggested"
    assert [d["statement"] for d in body["decisions"]] == ["Launch the pilot in November"]
    assert [q["statement"] for q in body["open_questions"]] == ["Who owns the vendor contract?"]
    assert body["decisions"][0]["evidence"][0]["start_ms"] == 11_000
    # Jordan's label has only a 0.7 AI proposal: the statement stays unresolved until confirmed.
    unresolved = body["items"]["unresolved"]
    assert [i["title"] for i in unresolved] == ["Send the revised budget"]
    assert body["items"]["theirs"] == []
    speakers = {s["label"]: s for s in body["speakers"]}
    assert speakers["Jordan"]["status"] == "proposed" and speakers["Jordan"]["person_id"] == str(jordan_id)
    assert h.request(other, "GET", f"/api/v1/meetings/{meeting_id}").status_code == 404

    # The user confirms both speakers (authority 5): Jordan's promise to the user is re-pointed
    # to "they owe me".
    put = h.request(
        avery,
        "PUT",
        f"/api/v1/meetings/{meeting_id}/speakers",
        json={
            "mappings": [
                {"label": "Jordan", "person_id": str(jordan_id)},
                {"label": "Avery", "person_id": str(avery.self_person_id)},
            ]
        },
        headers={"Idempotency-Key": "speakers-1"},
    )
    assert put.status_code == 200, put.text
    confirmed = {s["label"]: s for s in put.json()["speakers"]}["Jordan"]
    assert (confirmed["status"], confirmed["origin"]) == ("applied", "user")
    h.drain()
    body = h.request(avery, "GET", f"/api/v1/meetings/{meeting_id}").json()
    assert [i["title"] for i in body["items"]["theirs"]] == ["Send the revised budget"]
    assert body["items"]["unresolved"] == []
    duplicate = h.request(
        avery,
        "PUT",
        f"/api/v1/meetings/{meeting_id}/speakers",
        json={
            "mappings": [
                {"label": "Jordan", "person_id": str(jordan_id)},
                {"label": "Jordan", "person_id": None},
            ]
        },
    )
    assert duplicate.status_code == 422
    assert (
        h.request(
            other,
            "PUT",
            f"/api/v1/meetings/{meeting_id}/speakers",
            json={"mappings": [{"label": "Jordan", "person_id": None}]},
        ).status_code
        == 404
    )

    prep = h.request(avery, "GET", f"/api/v1/meetings/{meeting_id}/prep")
    assert prep.status_code == 200
    sections = prep.json()["sections"]
    assert [i["title"] for i in sections["open_items_theirs"]] == ["Send the revised budget"]
    assert prep.json()["asks"]["status"] == "not_requested"
    assert h.fake_ai.calls_for("MeetingAsks") == []  # the prep view never calls a model

    asked = h.request(avery, "POST", f"/api/v1/meetings/{meeting_id}/prep/asks")
    assert asked.status_code == 202 and asked.json()["asks"]["status"] == "pending"
    h.drain()
    ready = h.request(avery, "POST", f"/api/v1/meetings/{meeting_id}/prep/asks")
    assert ready.status_code == 200
    asks = ready.json()["asks"]
    assert asks["status"] == "ready" and asks["label"] == "AI suggestions"
    assert [a["text"] for a in asks["items"]] == ["Ask Jordan to confirm the budget date"]
    h.drain()
    assert len(h.fake_ai.calls_for("MeetingAsks")) == 1  # once per prep version
    assert h.request(other, "POST", f"/api/v1/meetings/{meeting_id}/prep/asks").status_code == 404
    assert h.request(other, "GET", f"/api/v1/meetings/{meeting_id}/prep").status_code == 404


def test_prep_asks_with_nothing_to_prepare_is_409(h: ApiHarness) -> None:
    avery = h.user("avery@brightwater.example", "Avery Lindqvist")
    rec_id = _upload(h, avery, b"WEBVTT\n\n00:01.000 --> 00:02.000\n<v Avery>Just me here.")
    h.drain()
    meeting_id = h.request(avery, "GET", f"/api/v1/recordings/{rec_id}").json()["meeting_id"]
    r = h.request(avery, "POST", f"/api/v1/meetings/{meeting_id}/prep/asks")
    assert r.status_code == 409 and r.json()["code"] == "nothing_to_ask"
    assert h.fake_ai.calls_for("MeetingAsks") == []
