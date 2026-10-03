"""RT-03: two workers apply extractions describing the same commitment (meeting + email)
concurrently (BACKEND_DESIGN.md §21, TECHNICAL_DESIGN.md §13.6).

"One work item with two evidence rows; no deadlock." Both extractions are made first with the
apply step held back; then both applies run at the same time on separate connections. The
per-user merge lock serializes them, so the second one matches the first one's item. Synthetic
data only.
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

import eca.work.pipeline as work_pipeline
from eca.connectors import NormalizedMessage, NormalizedPerson
from eca.intelligence.provider.types import GenerateRequest
from eca.platform.db import create_engine, create_session_factory
from eca.platform.uow import UnitOfWorkFactory
from eca.work.apply import apply_extraction
from eca.work.meeting_apply import apply_meeting_extraction
from tests.api.support import ApiHarness, api_harness
from tests.conftest import TempDatabase

pytestmark = pytest.mark.db

JORDAN = NormalizedPerson("jordan.blake@kestrel.example", "Jordan Blake")
PROMISE = "I will send the revised budget by Friday."
VTT = (
    "WEBVTT\n\n"
    f"00:00:01.000 --> 00:00:04.000\n<v Jordan>{PROMISE}\n\n"
    "00:00:05.000 --> 00:00:07.000\n<v Avery>Thanks, that works.\n"
).encode()


def _email_extraction(request: GenerateRequest) -> dict[str, Any]:
    return {
        "gist": "Jordan will send the revised budget.",
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
                "evidence_quote": PROMISE,
            }
        ],
    }


def _meeting_extraction(request: GenerateRequest) -> dict[str, Any]:
    return {
        "summary": "Budget follow-up.",
        "statements": [
            {
                "statement_kind": "promise",
                "action": "Send the revised budget",
                "speaker": "Jordan",
                "owner_ref": "speaker",
                "due_text": "by Friday",
                "confidence": 0.9,
                "evidence": {"segment_seq": 0, "quote": PROMISE},
            }
        ],
        "speaker_mapping": [
            {"label": "Jordan", "person_email": JORDAN.email, "confidence": 0.95, "quote": ""},
            {"label": "Avery", "person_email": "avery@brightwater.example", "confidence": 0.95, "quote": ""},
        ],
    }


@pytest.fixture
def h(isolated_db: TempDatabase, tmp_path: Path) -> Iterator[ApiHarness]:
    with api_harness(isolated_db, tmp_path) as harness:
        yield harness


async def _apply_both(h: ApiHarness, user_id: UUID, email_ext: UUID, meeting_ext: UUID) -> None:
    engine = create_engine(h.db.worker_url, pool_size=2, max_overflow=0)
    try:
        factory = UnitOfWorkFactory(create_session_factory(engine))
        now = datetime.datetime.now(datetime.UTC)

        async def run(fn: Any, ext_id: UUID) -> None:
            async with factory(user_id=user_id) as uow:
                await fn(uow, ext_id, now=now)

        await asyncio.wait_for(
            asyncio.gather(run(apply_extraction, email_ext), run(apply_meeting_extraction, meeting_ext)),
            timeout=60,
        )
    finally:
        await engine.dispose()


def test_rt03_meeting_and_email_apply_concurrently_into_one_item(
    h: ApiHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    held: list[str] = []

    async def hold(uow: Any, extraction_id: UUID, *, now: datetime.datetime) -> None:
        held.append(str(extraction_id))

    monkeypatch.setattr(work_pipeline, "apply_extraction", hold)
    monkeypatch.setattr(work_pipeline, "apply_meeting_extraction", hold)
    h.fake_ai.responders["EmailExtraction"] = _email_extraction
    h.fake_ai.responders["MeetingExtraction"] = _meeting_extraction
    avery = h.user("avery@brightwater.example", "Avery Lindqvist")

    h.connect_mail(
        avery,
        [
            NormalizedMessage(
                external_id="RT03-1",
                thread_external_id="<rt03@test.example>",
                rfc822_id="<rt03@test.example>",
                in_reply_to=None,
                sent_at=datetime.datetime(2026, 9, 21, 9, 0, tzinfo=datetime.UTC),
                sender=JORDAN,
                to=(NormalizedPerson(avery.email, "Avery Lindqvist"),),
                cc=(),
                subject="Budget",
                body_text=PROMISE,
                body_html=None,
                categories=("inbox",),
            )
        ],
    )
    r = h.request(
        avery,
        "POST",
        "/api/v1/recordings",
        json={
            "mime": "text/vtt",
            "bytes": len(VTT),
            "sha256": hashlib.sha256(VTT).hexdigest(),
            "occurred_at": "2026-09-21T10:00:00+00:00",
            "title": "Budget sync",
        },
        headers={"Idempotency-Key": "rt03"},
    )
    upload = r.json()["upload"]
    assert h.client.put(upload["url"], content=VTT, headers=upload["headers"]).status_code == 201
    rec_id = r.json()["recording"]["id"]
    assert h.request(avery, "POST", f"/api/v1/recordings/{rec_id}/complete").status_code == 202
    h.drain()

    exts = dict(h.rows("SELECT pipeline, id FROM extractions WHERE status = 'succeeded'"))
    assert set(exts) == {"email_extract", "meeting_extract"}
    assert h.scalar("SELECT count(*) FROM work_items") == 0  # both applies held back

    monkeypatch.undo()
    asyncio.run(_apply_both(h, avery.user_id, exts["email_extract"], exts["meeting_extract"]))
    h.drain()

    assert h.scalar("SELECT count(*) FROM work_items") == 1
    assert h.scalar("SELECT count(*) FROM item_evidence") == 2
    kinds = {
        k for (k,) in h.rows("SELECT s.kind FROM evidence e JOIN source_items s ON s.id = e.source_item_id")
    }
    assert kinds == {"message", "transcript_file"}
    assert h.rows("SELECT apply_status FROM extractions ORDER BY pipeline") == [("applied",), ("applied",)]
    direction = h.scalar("SELECT direction FROM work_items")
    assert direction == "waiting_for"
