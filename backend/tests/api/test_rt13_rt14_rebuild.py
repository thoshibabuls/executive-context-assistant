"""RT-13 and RT-14: the R2 rebuild (BACKEND_DESIGN.md §8.5, §21).

RT-13: "R2 rebuild after user corrections: all user-authored values and user-touched item IDs
preserved." RT-14: "R2 rebuild equivalence: rebuilt AI-derived state equals pre-rebuild state
(excluding IDs of AI-only items)." The state covers mail and meeting extractions, items, decisions,
open questions, evidence and their links. No model call is made by R2. Synthetic data only.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from eca.connectors import NormalizedMessage, NormalizedPerson
from eca.intelligence.provider.types import GenerateRequest
from eca.platform.db import create_engine, create_session_factory
from eca.platform.uow import UnitOfWorkFactory
from eca.work.recompute import reapply_all
from tests.api.support import ApiHarness, ApiUser, api_harness
from tests.conftest import TempDatabase

pytestmark = pytest.mark.db

JORDAN = NormalizedPerson("jordan.blake@kestrel.example", "Jordan Blake")
SAM = NormalizedPerson("sam.okafor@halvorsen.example", "Sam Okafor")
MAIL_1 = "I will send the revised budget by Friday. We decided to drop the Q4 offsite."
MAIL_2 = "Could you review the vendor contract by Monday? Who signs the renewal?"
VTT = (
    b"WEBVTT\n\n"
    b"00:00:01.000 --> 00:00:04.000\n<v Jordan>I will book the venue by Thursday.\n\n"
    b"00:00:05.000 --> 00:00:08.000\n<v Avery>We agreed to hire a second analyst.\n"
)


def _triage() -> dict[str, Any]:
    return {
        "category": "action",
        "needs_reply": True,
        "request_type": "reply",
        "business_impact": "medium",
        "confidence": 0.9,
    }


def _email(request: GenerateRequest) -> dict[str, Any]:
    prompt = str(request.contents[0])
    if MAIL_1 in prompt:
        return {
            "gist": "Budget and offsite.",
            "triage": _triage(),
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
                    "kind": "decision",
                    "statement": "Drop the Q4 offsite",
                    "evidence_quote": "We decided to drop the Q4 offsite.",
                    "confidence": 0.9,
                }
            ],
        }
    return {
        "gist": "Contract review.",
        "triage": _triage(),
        "statements": [
            {
                "statement_kind": "request",
                "action": "Review the vendor contract",
                "owner_ref": "recipient:avery@brightwater.example",
                "due_text": "by Monday",
                "confidence": 0.9,
                "evidence_quote": "Could you review the vendor contract by Monday?",
            }
        ],
        "decisions": [
            {
                "kind": "open_question",
                "statement": "Who signs the renewal?",
                "evidence_quote": "Who signs the renewal?",
                "confidence": 0.8,
            }
        ],
    }


def _meeting(request: GenerateRequest) -> dict[str, Any]:
    return {
        "summary": "Venue and hiring.",
        "statements": [
            {
                "statement_kind": "promise",
                "action": "Book the venue",
                "speaker": "Jordan",
                "owner_ref": "speaker",
                "due_text": "by Thursday",
                "confidence": 0.9,
                "evidence": {"segment_seq": 0, "quote": "I will book the venue by Thursday."},
            }
        ],
        "decisions": [
            {
                "kind": "decision",
                "statement": "Hire a second analyst",
                "confidence": 0.9,
                "evidence": {"segment_seq": 1, "quote": "hire a second analyst"},
            }
        ],
        "speaker_mapping": [
            {"label": "Jordan", "person_email": JORDAN.email, "confidence": 0.95, "quote": ""},
            {"label": "Avery", "person_email": "avery@brightwater.example", "confidence": 0.95, "quote": ""},
        ],
    }


def _mail(n: int, sender: NormalizedPerson, body: str, to: str) -> NormalizedMessage:
    return NormalizedMessage(
        external_id=f"RT13-{n}",
        thread_external_id=f"<rt13-{n}@test.example>",
        rfc822_id=f"<rt13-{n}@test.example>",
        in_reply_to=None,
        sent_at=datetime.datetime(2026, 9, 21, 9, n, tzinfo=datetime.UTC),
        sender=sender,
        to=(NormalizedPerson(to, "Avery Lindqvist"),),
        cc=(),
        subject=f"Topic {n}",
        body_text=body,
        body_html=None,
        categories=("inbox",),
    )


def _setup(h: ApiHarness) -> ApiUser:
    h.fake_ai.responders["EmailExtraction"] = _email
    h.fake_ai.responders["MeetingExtraction"] = _meeting
    avery = h.user("avery@brightwater.example", "Avery Lindqvist")
    h.connect_mail(avery, [_mail(1, JORDAN, MAIL_1, avery.email), _mail(2, SAM, MAIL_2, avery.email)])
    r = h.request(
        avery,
        "POST",
        "/api/v1/recordings",
        json={
            "mime": "text/vtt",
            "bytes": len(VTT),
            "sha256": hashlib.sha256(VTT).hexdigest(),
            "occurred_at": "2026-09-21T11:00:00+00:00",
            "title": "Planning",
        },
        headers={"Idempotency-Key": "rt13"},
    )
    upload = r.json()["upload"]
    assert h.client.put(upload["url"], content=VTT, headers=upload["headers"]).status_code == 201
    assert (
        h.request(avery, "POST", f"/api/v1/recordings/{r.json()['recording']['id']}/complete").status_code
        == 202
    )
    h.drain()
    return avery


ITEMS = (
    "SELECT title, type, direction, due_at, due_text, lifecycle_status, verification_status, origin, "
    "archived, confidence_band, user_fields FROM work_items ORDER BY title"
)
DECISIONS = (
    "SELECT statement, kind, origin, verification_status, decided_at, meeting_id IS NOT NULL, user_fields "
    "FROM decisions ORDER BY statement"
)
LINKS = (
    "SELECT coalesce(wi.title, d.statement), ie.item_type, ie.relation, ev.id, ev.quote "
    "FROM item_evidence ie JOIN evidence ev ON ev.id = ie.evidence_id "
    "LEFT JOIN work_items wi ON wi.id = ie.item_id "
    "LEFT JOIN decisions d ON d.id = ie.item_id ORDER BY 1, 2, 4"
)


def _state(h: ApiHarness) -> dict[str, Any]:
    return {
        "items": h.rows(ITEMS),
        "decisions": h.rows(DECISIONS),
        "links": h.rows(LINKS),
        "evidence": h.rows("SELECT id, quote, source_item_id FROM evidence ORDER BY id"),
        "extractions": h.rows("SELECT id, apply_status FROM extractions ORDER BY id"),
    }


def _rebuild(h: ApiHarness, user_id: UUID) -> None:
    async def run() -> None:
        engine = create_engine(h.db.worker_url, pool_size=1, max_overflow=0)
        try:
            async with UnitOfWorkFactory(create_session_factory(engine))(user_id=user_id) as uow:
                await reapply_all(uow, now=datetime.datetime.now(datetime.UTC))
        finally:
            await engine.dispose()

    calls = len(h.fake_ai.generate_calls)
    asyncio.run(run())
    h.drain()
    assert len(h.fake_ai.generate_calls) == calls  # R2 makes no model call


@pytest.fixture
def h(isolated_db: TempDatabase, tmp_path: Path) -> Iterator[ApiHarness]:
    with api_harness(isolated_db, tmp_path) as harness:
        yield harness


def test_rt14_rebuild_reproduces_the_ai_derived_state(h: ApiHarness) -> None:
    avery = _setup(h)
    before = _state(h)
    assert len(before["items"]) == 3 and len(before["decisions"]) == 3
    assert {k for (_, k, *_) in before["decisions"]} == {"decision", "open_question"}
    _rebuild(h, avery.user_id)
    assert _state(h) == before


def test_rt13_rebuild_keeps_user_corrections_and_touched_ids(h: ApiHarness) -> None:
    avery = _setup(h)
    items = dict(h.rows("SELECT title, id FROM work_items"))
    decisions = dict(h.rows("SELECT statement, id FROM decisions"))

    budget = items["Send the revised budget"]
    contract = items["Review the vendor contract"]
    venue = items["Book the venue"]  # a meeting-derived item
    assert h.request(avery, "POST", f"/api/v1/work-items/{budget}/confirm").status_code == 200
    assert h.request(avery, "POST", f"/api/v1/work-items/{venue}/confirm").status_code == 200
    patched = h.request(
        avery,
        "PATCH",
        f"/api/v1/work-items/{contract}",
        json={"due_at": "2026-09-30T17:00:00+00:00", "title": "Review and sign the vendor contract"},
        headers={"If-Match": h.request(avery, "GET", f"/api/v1/work-items/{contract}").headers["ETag"]},
    )
    assert patched.status_code == 200, patched.text
    offsite = decisions["Drop the Q4 offsite"]
    assert h.request(avery, "POST", f"/api/v1/decisions/{offsite}/confirm").status_code == 200
    question = decisions["Who signs the renewal?"]
    assert (
        h.request(avery, "PATCH", f"/api/v1/decisions/{question}", json={"notes": "Ask legal"}).status_code
        == 200
    )
    h.drain()
    before = _state(h)
    touched_items = {budget, contract, venue}
    touched_decisions = {offsite, question}

    _rebuild(h, avery.user_id)
    after = _state(h)
    assert {UUID(str(i)) for (i,) in h.rows("SELECT id FROM work_items")} >= touched_items
    assert {UUID(str(i)) for (i,) in h.rows("SELECT id FROM decisions")} >= touched_decisions
    row = h.rows("SELECT title, due_at, verification_status FROM work_items WHERE id = %s", (contract,))[0]
    assert row[0] == "Review and sign the vendor contract"
    assert row[1] == datetime.datetime(2026, 9, 30, 17, 0, tzinfo=datetime.UTC)
    assert h.scalar("SELECT verification_status FROM work_items WHERE id = %s", (budget,)) == "confirmed"
    assert h.scalar("SELECT verification_status FROM work_items WHERE id = %s", (venue,)) == "confirmed"
    assert h.scalar("SELECT count(*) FROM work_items") == 3  # no duplicate of a kept item
    assert h.scalar("SELECT verification_status FROM decisions WHERE id = %s", (offsite,)) == "confirmed"
    assert h.scalar("SELECT notes FROM decisions WHERE id = %s", (question,)) == "Ask legal"
    assert h.scalar("SELECT count(*) FROM decisions") == 3  # no duplicate of a kept decision
    assert after["items"] == before["items"]
    assert after["decisions"] == before["decisions"]
    assert after["links"] == before["links"]
