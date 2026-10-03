"""S4 meeting preparation in chat reads the stored prep sections (CONTEXT_ARCHITECTURE.md §9.11).

An upcoming calendar meeting with an attendee who promised something by email and asked an open
question. Once the prep sections are stored (the prep view's ``POST /prep/asks``), a
meeting-scoped chat question is answered from the same sections: the attendee's open item and the
thread's unresolved question are both in the packet. Synthetic data only.
"""

from __future__ import annotations

import datetime
import json
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
BODY = "I will send the revised budget by Friday. Who signs the vendor renewal?"


def _email(request: GenerateRequest) -> dict[str, Any]:
    return {
        "gist": "Budget and renewal.",
        "triage": {
            "category": "action",
            "needs_reply": True,
            "request_type": "reply",
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
        "decisions": [
            {
                "kind": "open_question",
                "statement": "Who signs the vendor renewal?",
                "evidence_quote": "Who signs the vendor renewal?",
                "confidence": 0.8,
            }
        ],
    }


@pytest.fixture
def h(isolated_db: TempDatabase, tmp_path: Path) -> Iterator[ApiHarness]:
    with api_harness(isolated_db, tmp_path) as harness:
        yield harness


def _events(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.split("\n") if ": " in line)
        out[lines["event"]] = json.loads(lines["data"])
    return out


def test_meeting_scoped_chat_answers_from_the_stored_prep_sections(h: ApiHarness) -> None:
    h.fake_ai.responders["EmailExtraction"] = _email
    avery = h.user("avery@brightwater.example", "Avery Lindqvist")
    now = datetime.datetime.now(datetime.UTC)
    h.connect_mail(
        avery,
        [
            NormalizedMessage(
                external_id="S4-1",
                thread_external_id="<s4@test.example>",
                rfc822_id="<s4@test.example>",
                in_reply_to=None,
                sent_at=now - datetime.timedelta(days=2),
                sender=JORDAN,
                to=(NormalizedPerson(avery.email, "Avery Lindqvist"),),
                cc=(),
                subject="Budget",
                body_text=BODY,
                body_html=None,
                categories=("inbox",),
            )
        ],
    )
    start = (now + datetime.timedelta(hours=20)).replace(minute=0, second=0, microsecond=0)
    h.sync_calendar(
        avery,
        [
            NormalizedEvent(
                external_id="EV-1",
                series_external_id=None,
                ical_uid="ev-1@test.example",
                start=start,
                end=start + datetime.timedelta(minutes=30),
                timezone="UTC",
                title="Budget review",
                description="Review the revised budget.",
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
    meeting_id = h.scalar("SELECT id FROM meetings WHERE title = 'Budget review'")
    prep = h.request(avery, "POST", f"/api/v1/meetings/{meeting_id}/prep/asks")
    assert prep.status_code in (200, 202), prep.text
    sections = prep.json()["sections"]
    assert [i["title"] for i in sections["open_items_theirs"]] == ["Send the revised budget"]
    assert [q["statement"] for q in sections["unresolved_questions"]] == ["Who signs the vendor renewal?"]

    session = h.request(
        avery,
        "POST",
        "/api/v1/chat/sessions",
        json={"scope": {"kind": "meeting", "meeting_id": str(meeting_id)}},
    )
    assert session.status_code == 201, session.text
    r = h.request(
        avery,
        "POST",
        f"/api/v1/chat/sessions/{session.json()['id']}/messages",
        json={"text": "Prepare me for this meeting"},
        headers={"Idempotency-Key": "s4"},
    )
    events = _events(r.text)
    assert events["plan"]["scenario"] == "S4"
    texts = [c["text"] for c in events["sources"]["citations"]]
    assert any("Send the revised budget" in t for t in texts)
    assert any("Who signs the vendor renewal?" in t for t in texts)
