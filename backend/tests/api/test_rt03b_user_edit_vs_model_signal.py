"""RT-03b: the user edits ``due_at`` while a model ``new_deadline`` signal applies
(BACKEND_DESIGN.md §21; authority rules, TECHNICAL_DESIGN.md §13).

"User value kept; ``conflict_detected`` event; no lost events; versions monotonic." The model
signal comes from a later email that contradicts the user's date; its apply and the user's edit
run at the same time on separate connections. Synthetic data only.
"""

from __future__ import annotations

import asyncio
import datetime
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
from eca.work.service import user_edit
from tests.api.support import ApiHarness, api_harness
from tests.conftest import TempDatabase

pytestmark = pytest.mark.db

JORDAN = NormalizedPerson("jordan.blake@kestrel.example", "Jordan Blake")
FIRST = "I will send the revised budget by Friday."
SECOND = "Update: the revised budget will come next Wednesday instead."
USER_EDIT_AT = datetime.datetime(2026, 9, 21, 12, 0, tzinfo=datetime.UTC)
USER_DUE = datetime.datetime(2026, 9, 24, 17, 0, tzinfo=datetime.UTC)


def _mail(n: int, body: str, sent: datetime.datetime, to: str) -> NormalizedMessage:
    return NormalizedMessage(
        external_id=f"RT03B-{n}",
        thread_external_id="<rt03b@test.example>",
        rfc822_id=f"<rt03b-{n}@test.example>",
        in_reply_to=None if n == 1 else "<rt03b-1@test.example>",
        sent_at=sent,
        sender=JORDAN,
        to=(NormalizedPerson(to, "Avery Lindqvist"),),
        cc=(),
        subject="Budget",
        body_text=body,
        body_html=None,
        categories=("inbox",),
    )


def _extraction(request: GenerateRequest) -> dict[str, Any]:
    prompt = str(request.contents[0])
    base = {
        "gist": "Budget timing.",
        "triage": {
            "category": "action",
            "needs_reply": False,
            "request_type": "none",
            "business_impact": "medium",
            "confidence": 0.9,
        },
    }
    if SECOND in prompt:
        assert "C1:" in prompt  # the open item is offered as a candidate
        return {
            **base,
            "status_signals": [
                {
                    "candidate_id": "C1",
                    "signal": "new_deadline",
                    "new_due_text": "next Wednesday",
                    "evidence_quote": SECOND,
                    "confidence": 0.95,
                }
            ],
        }
    return {
        **base,
        "statements": [
            {
                "statement_kind": "promise",
                "action": "Send the revised budget",
                "owner_ref": "speaker",
                "due_text": "by Friday",
                "confidence": 0.9,
                "evidence_quote": FIRST,
            }
        ],
    }


@pytest.fixture
def h(isolated_db: TempDatabase, tmp_path: Path) -> Iterator[ApiHarness]:
    with api_harness(isolated_db, tmp_path) as harness:
        yield harness


async def _race(h: ApiHarness, user_id: UUID, item_id: UUID, extraction_id: UUID) -> None:
    engine = create_engine(h.db.worker_url, pool_size=2, max_overflow=0)
    api = create_engine(h.db.runtime_url, pool_size=1, max_overflow=0)
    try:
        worker = UnitOfWorkFactory(create_session_factory(engine))
        api_factory = UnitOfWorkFactory(create_session_factory(api))

        async def edit() -> None:
            async with api_factory(user_id=user_id) as uow:
                await user_edit(uow, item_id, {"due_at": USER_DUE}, request_key="rt03b-edit", at=USER_EDIT_AT)

        async def apply() -> None:
            async with worker(user_id=user_id) as uow:
                await apply_extraction(uow, extraction_id, now=datetime.datetime.now(datetime.UTC))

        await asyncio.wait_for(asyncio.gather(edit(), apply()), timeout=60)
    finally:
        await engine.dispose()
        await api.dispose()


def test_rt03b_user_due_date_survives_a_concurrent_model_deadline_signal(
    h: ApiHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h.fake_ai.responders["EmailExtraction"] = _extraction
    avery = h.user("avery@brightwater.example", "Avery Lindqvist")
    h.connect_mail(
        avery, [_mail(1, FIRST, datetime.datetime(2026, 9, 21, 9, 0, tzinfo=datetime.UTC), avery.email)]
    )
    h.drain()
    item_id = h.scalar("SELECT id FROM work_items")
    version_before = h.scalar("SELECT version FROM work_items")

    held: list[UUID] = []

    async def hold(uow: Any, extraction_id: UUID, *, now: datetime.datetime) -> None:
        held.append(extraction_id)

    monkeypatch.setattr(work_pipeline, "apply_extraction", hold)
    h.accounts.mail_feed(avery.email).add(
        _mail(2, SECOND, datetime.datetime(2026, 9, 22, 9, 0, tzinfo=datetime.UTC), avery.email)
    )
    asyncio.run(_sync_again(h, avery))
    h.drain()
    assert len(held) == 1
    monkeypatch.undo()

    asyncio.run(_race(h, avery.user_id, item_id, held[0]))
    h.drain()

    assert h.scalar("SELECT due_at FROM work_items") == USER_DUE  # user value kept
    events = [e for (e,) in h.rows("SELECT event_type FROM context_events WHERE entity_id = %s", (item_id,))]
    assert {"created", "user_edit", "conflict_detected"} <= set(events)
    assert any(e not in ("created", "user_edit", "conflict_detected") for e in events)  # the model signal
    assert h.scalar("SELECT version FROM work_items") > version_before
    versions = [
        v
        for (v,) in h.rows(
            "SELECT (payload->>'version')::int FROM outbox WHERE event_type = 'WorkItemChanged' "
            "AND aggregate_id = %s ORDER BY id",  # uuid7 ids are made after the row lock: commit order
            (item_id,),
        )
    ]
    assert versions == sorted(versions) and len(set(versions)) == len(versions)
    assert versions[-1] == h.scalar("SELECT version FROM work_items")


async def _sync_again(h: ApiHarness, avery: Any) -> None:
    from eca.ingestion import sync_mail

    engine = create_engine(h.db.worker_url, pool_size=2, max_overflow=0)
    try:
        connection_id = h.scalar("SELECT id FROM connections WHERE user_id = %s", (avery.user_id,))
        await sync_mail(
            UnitOfWorkFactory(create_session_factory(engine)),
            h.connectors,
            user_id=avery.user_id,
            connection_id=connection_id,
            now=datetime.datetime.now(datetime.UTC),
            owner="rt03b",
        )
    finally:
        await engine.dispose()
