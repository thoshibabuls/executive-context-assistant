"""Speaker confirmation re-points status signals (BACKEND_DESIGN.md §16.9; known Phase 4 gap).

Jordan promised a budget by email. In a later calendar meeting Jordan (an unmapped transcript
label) says it was sent: AI-10 reports a completion claim on that item. Before the user confirms
the speaker the claim carries authority 2 and no speaker; after confirmation the model event is
appended again with Jordan as the speaker and authority 4 (the owner), still a model claim.
Synthetic data only.
"""

from __future__ import annotations

import datetime
import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from eca.connectors import NormalizedAttendee, NormalizedEvent, NormalizedMessage, NormalizedPerson
from eca.intelligence.provider.types import GenerateRequest
from tests.api.support import ApiHarness, api_harness
from tests.conftest import TempDatabase

pytestmark = pytest.mark.db

JORDAN = NormalizedPerson("jordan.blake@kestrel.example", "Jordan Blake")
CLAIM = "I sent the revised budget this morning."


def _email(request: GenerateRequest) -> dict[str, Any]:
    return {
        "gist": "Budget promise.",
        "triage": {
            "category": "action",
            "needs_reply": False,
            "request_type": "none",
            "business_impact": "medium",
            "confidence": 0.9,
        },
        "statements": [
            {
                "statement_kind": "promise",
                "action": "Send the revised budget",
                "owner_ref": "speaker",
                "due_text": "by Friday",
                "confidence": 0.9,
                "evidence_quote": "I will send the revised budget by Friday.",
            }
        ],
    }


def _meeting(request: GenerateRequest) -> dict[str, Any]:
    assert "C1:" in str(request.contents[0])  # Jordan's open item is a candidate
    return {
        "summary": "Budget status.",
        "status_signals": [
            {
                "candidate_id": "C1",
                "signal": "completed_claim",
                "speaker": "Jordan",
                "confidence": 0.9,
                "evidence": {"segment_seq": 0, "quote": CLAIM},
            }
        ],
        "speaker_mapping": [
            {"label": "Jordan", "person_email": JORDAN.email, "confidence": 0.7, "quote": ""}
        ],
    }


@pytest.fixture
def h(isolated_db: TempDatabase, tmp_path: Path) -> Iterator[ApiHarness]:
    with api_harness(isolated_db, tmp_path) as harness:
        yield harness


def _claims(h: ApiHarness, item_id: Any) -> list[tuple[Any, ...]]:
    return h.rows(
        "SELECT authority, payload->>'by_person', coalesce(payload->>'speaker_remapped', 'false') "
        "FROM context_events WHERE entity_id = %s AND event_type = 'completed_claim' "
        "ORDER BY recorded_at, id",
        (item_id,),
    )


def test_confirming_a_speaker_re_points_their_status_signals(h: ApiHarness) -> None:
    h.fake_ai.responders["EmailExtraction"] = _email
    h.fake_ai.responders["MeetingExtraction"] = _meeting
    avery = h.user("avery@brightwater.example", "Avery Lindqvist")
    now = datetime.datetime.now(datetime.UTC)
    h.connect_mail(
        avery,
        [
            NormalizedMessage(
                external_id="SIG-1",
                thread_external_id="<sig@test.example>",
                rfc822_id="<sig@test.example>",
                in_reply_to=None,
                sent_at=now - datetime.timedelta(days=2),
                sender=JORDAN,
                to=(NormalizedPerson(avery.email, "Avery Lindqvist"),),
                cc=(),
                subject="Budget",
                body_text="I will send the revised budget by Friday.",
                body_html=None,
                categories=("inbox",),
            )
        ],
    )
    start = (now - datetime.timedelta(hours=2)).replace(second=0, microsecond=0)
    h.sync_calendar(
        avery,
        [
            NormalizedEvent(
                external_id="EV-SIG",
                series_external_id=None,
                ical_uid="ev-sig@test.example",
                start=start,
                end=start + datetime.timedelta(minutes=30),
                timezone="UTC",
                title="Budget check-in",
                description=None,
                attendees=(
                    NormalizedAttendee(avery.email, "Avery Lindqvist", "accepted"),
                    NormalizedAttendee(JORDAN.email, "Jordan Blake", "accepted"),
                ),
                organizer=NormalizedPerson(avery.email, "Avery Lindqvist"),
                conference_uri=None,
                status="confirmed",
            )
        ],
    )
    h.drain()
    item_id = h.scalar("SELECT id FROM work_items")
    jordan_id = h.scalar("SELECT owner_person_id FROM work_items")
    meeting_id = h.scalar("SELECT id FROM meetings WHERE title = 'Budget check-in'")

    vtt = f"WEBVTT\n\n00:00:01.000 --> 00:00:04.000\n<v Jordan>{CLAIM}\n".encode()
    r = h.request(
        avery,
        "POST",
        "/api/v1/recordings",
        json={
            "mime": "text/vtt",
            "bytes": len(vtt),
            "sha256": hashlib.sha256(vtt).hexdigest(),
            "meeting_id": str(meeting_id),
        },
        headers={"Idempotency-Key": "sig"},
    )
    upload = r.json()["upload"]
    assert h.client.put(upload["url"], content=vtt, headers=upload["headers"]).status_code == 201
    assert (
        h.request(avery, "POST", f"/api/v1/recordings/{r.json()['recording']['id']}/complete").status_code
        == 202
    )
    h.drain()
    assert _claims(h, item_id) == [(2, None, "false")]  # unmapped speaker: authority 2

    put = h.request(
        avery,
        "PUT",
        f"/api/v1/meetings/{meeting_id}/speakers",
        json={"mappings": [{"label": "Jordan", "person_id": str(jordan_id)}]},
        headers={"Idempotency-Key": "confirm-jordan"},
    )
    assert put.status_code == 200, put.text
    h.drain()
    assert _claims(h, item_id) == [(2, None, "false"), (4, str(jordan_id), "true")]
    assert h.scalar("SELECT reported_status FROM work_items") is not None  # still a claim
    assert h.scalar("SELECT lifecycle_status FROM work_items") == "open"  # never a fact
    # Confirming again is idempotent.
    h.request(
        avery,
        "PUT",
        f"/api/v1/meetings/{meeting_id}/speakers",
        json={"mappings": [{"label": "Jordan", "person_id": str(jordan_id)}]},
        headers={"Idempotency-Key": "confirm-jordan-2"},
    )
    assert len(_claims(h, item_id)) == 2
