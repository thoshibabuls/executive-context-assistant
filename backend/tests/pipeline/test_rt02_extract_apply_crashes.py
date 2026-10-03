"""RT-02 and RT-02b: crashes around the AI extraction commit (BACKEND_DESIGN.md §8.1, §21).

RT-02: crash after the extraction commit, before apply. "Apply runs via job or reconciler; one
``extractions`` row; AI call count = 1; state equals the no-crash run."
RT-02b: crash after the AI response, before the extraction commit. "At most one extra AI call; one
extraction row; final state identical."

The crash is a failure inside the next transaction (``apply_extraction`` or ``store_success``);
recovery is the source-item stage reconciler, as after a lost job. Synthetic messages only.
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import pytest

import eca.work.pipeline as work_pipeline
from eca.connectors import NormalizedMessage, NormalizedPerson
from eca.ingestion import reconcile_user_stages, sync_mail
from eca.intelligence.provider.types import GenerateRequest
from tests.conftest import TempDatabase, new_database
from tests.pipeline.support import Pipeline, Tenant, pipeline

pytestmark = pytest.mark.db

AVERY = NormalizedPerson("avery@brightwater.example", "Avery Lindqvist")
# subject: (sender, body, statement kind, action, due text)
MAILS = {
    "Budget": (
        NormalizedPerson("jordan.blake@kestrel.example", "Jordan Blake"),
        "I will send the revised budget by Friday.",
        "promise",
        "Send the revised budget",
        "by Friday",
    ),
    "Venue": (
        NormalizedPerson("sam.okafor@halvorsen.example", "Sam Okafor"),
        "Could you confirm the venue by Monday?",
        "request",
        "Confirm the venue",
        "by Monday",
    ),
    "Deck": (
        NormalizedPerson("priya.raman@brightwater.example", "Priya Raman"),
        "I will share the board deck tomorrow.",
        "promise",
        "Share the board deck",
        "tomorrow",
    ),
}


def _messages() -> list[NormalizedMessage]:
    out = []
    for i, (subject, (sender, body, _, _, _)) in enumerate(MAILS.items()):
        out.append(
            NormalizedMessage(
                external_id=f"RT02-{i}",
                thread_external_id=f"<rt02-{i}@test.example>",
                rfc822_id=f"<rt02-{i}@test.example>",
                in_reply_to=None,
                sent_at=datetime.datetime(2026, 9, 21, 9, i, tzinfo=datetime.UTC),
                sender=sender,
                to=(AVERY,),
                cc=(),
                subject=subject,
                body_text=body,
                body_html=None,
                categories=("inbox",),
            )
        )
    return out


def _extraction(request: GenerateRequest) -> dict[str, Any]:
    prompt = str(request.contents[0])
    subject = re.search(r"^SUBJECT: (.*)$", prompt, re.MULTILINE)
    assert subject is not None
    _, body, kind, action, due_text = MAILS[subject.group(1)]
    return {
        "gist": action,
        "triage": {
            "category": "action",
            "needs_reply": kind == "request",
            "request_type": "reply" if kind == "request" else "none",
            "business_impact": "medium",
            "confidence": 0.9,
        },
        "statements": [
            {
                "statement_kind": kind,
                "action": action,
                "owner_ref": "recipient:avery@brightwater.example" if kind == "request" else "speaker",
                "due_text": due_text,
                "confidence": 0.9,
                "evidence_quote": body,
            }
        ],
    }


STATE_QUERIES = {
    "items": "SELECT title, type, direction, due_text, lifecycle_status, verification_status, archived "
    "FROM work_items ORDER BY title",
    "evidence": "SELECT quote FROM evidence ORDER BY quote",
    "events": "SELECT ce.event_type, wi.title FROM context_events ce "
    "JOIN work_items wi ON wi.id = ce.entity_id "
    "WHERE ce.entity_type = 'work_item' ORDER BY wi.title, ce.event_type",
    "extractions": "SELECT status, apply_status, count(*) FROM extractions GROUP BY 1, 2 ORDER BY 1, 2",
    "stages": "SELECT stage, count(*) FROM source_items GROUP BY stage ORDER BY stage",
}


def _state(p: Pipeline) -> dict[str, list[tuple[Any, ...]]]:
    return {name: p.rows(q) for name, q in STATE_QUERIES.items()}


async def _start(p: Pipeline) -> Tenant:
    p.fake_ai.responders["EmailExtraction"] = _extraction
    t = await p.tenant()
    p.feed(t).extend(_messages())
    await sync_mail(
        p.worker,
        p.connectors,
        user_id=t.user_id,
        connection_id=t.connection_id,
        now=p.clock.now(),
        owner="rt02",
    )
    return t


async def _recover(p: Pipeline, t: Tenant) -> None:
    """After the crash: the stage reconciler re-publishes stuck items, the worker runs them."""
    p.rows("UPDATE source_items SET stage_updated_at = now() - interval '1 hour' RETURNING 1")
    async with p.worker(user_id=t.user_id) as uow:
        await reconcile_user_stages(uow, now=datetime.datetime.now(datetime.UTC))
    await p.executor().drain(batch=1)


def _fail_once(target: Callable[..., Awaitable[Any]]) -> tuple[Callable[..., Awaitable[Any]], list[int]]:
    calls: list[int] = []

    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("simulated crash")
        return await target(*args, **kwargs)

    return wrapper, calls


@pytest.fixture
def baseline_db(admin_base_url: str) -> Iterator[TempDatabase]:
    """A second database for the no-crash run (migrated outside the test's event loop)."""
    with new_database(admin_base_url, migrate=True) as db:
        yield db


async def _baseline(db: TempDatabase) -> dict[str, list[tuple[Any, ...]]]:
    async with pipeline(db) as p:
        await _start(p)
        await p.drain()
        return _state(p)


async def test_rt02_crash_after_extraction_commit_before_apply(
    isolated_db: TempDatabase, baseline_db: TempDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = await _baseline(baseline_db)
    assert len(expected["items"]) == 3 and expected["stages"] == [("applied", 3)]
    async with pipeline(isolated_db) as p:
        t = await _start(p)
        crashing, calls = _fail_once(work_pipeline.apply_extraction)
        monkeypatch.setattr(work_pipeline, "apply_extraction", crashing)
        with pytest.raises(RuntimeError, match="simulated crash"):
            await p.executor().drain(batch=1)
        assert p.scalar("SELECT count(*) FROM extractions WHERE apply_status = 'pending'") >= 1
        await _recover(p, t)
        assert _state(p) == expected
        assert p.scalar("SELECT count(*) FROM extractions") == 3
        assert len(p.fake_ai.calls_for("EmailExtraction")) == 3  # AI call count 1 per message
        assert len(calls) >= 2


async def test_rt02b_crash_after_ai_response_before_extraction_commit(
    isolated_db: TempDatabase, baseline_db: TempDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = await _baseline(baseline_db)
    async with pipeline(isolated_db) as p:
        t = await _start(p)
        crashing, _ = _fail_once(work_pipeline.store_success)
        monkeypatch.setattr(work_pipeline, "store_success", crashing)
        with pytest.raises(RuntimeError, match="simulated crash"):
            await p.executor().drain(batch=1)
        await _recover(p, t)
        assert _state(p) == expected
        assert p.scalar("SELECT count(*) FROM extractions") == 3
        assert len(p.fake_ai.calls_for("EmailExtraction")) <= 4  # at most one extra call
