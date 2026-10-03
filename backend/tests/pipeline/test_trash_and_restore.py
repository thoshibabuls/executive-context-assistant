"""Trash and Spam at the provider (BACKEND_DESIGN.md §9.2): a message moved to Trash leaves
needs-response and retrieval, its follow-up reminder is re-evaluated and its items keep their
evidence; a restore reverses it. A message already in Spam when imported never awaits a reply.
Synthetic data only.
"""

from __future__ import annotations

import datetime
from typing import Any

import pytest

from eca.connectors import NormalizedMessage, NormalizedPerson
from eca.ingestion import sync_mail
from eca.intelligence.provider.types import GenerateRequest
from tests.conftest import TempDatabase
from tests.pipeline.support import Pipeline, Tenant, pipeline

pytestmark = pytest.mark.db

RAJ = NormalizedPerson("raj.menon@kestrelbank.example", "Raj Menon")
BODY = "Could you send the Q3 numbers by Friday?"


def _extraction(request: GenerateRequest) -> dict[str, Any]:
    return {
        "gist": "Asks for numbers.",
        "triage": {
            "category": "action",
            "needs_reply": True,
            "request_type": "provide_info",
            "business_impact": "medium",
            "confidence": 0.9,
        },
        "statements": [
            {
                "statement_kind": "request",
                "action": "Send the Q3 numbers",
                "owner_ref": "recipient:avery@brightwater.example",
                "due_text": "by Friday",
                "confidence": 0.9,
                "evidence_quote": BODY,
            }
        ],
    }


def _mail(t: Tenant, ext: str, categories: tuple[str, ...] = ("inbox",)) -> NormalizedMessage:
    return NormalizedMessage(
        external_id=ext,
        thread_external_id=f"<{ext}@test.example>",
        rfc822_id=f"<{ext}@test.example>",
        in_reply_to=None,
        sent_at=datetime.datetime(2026, 9, 21, 9, 0, tzinfo=datetime.UTC),
        sender=RAJ,
        to=(NormalizedPerson(t.email, "Avery Lindqvist"),),
        cc=(),
        subject="Numbers",
        body_text=BODY,
        body_html=None,
        categories=categories,
    )


async def _sync(p: Pipeline, t: Tenant, owner: str) -> None:
    await sync_mail(
        p.worker,
        p.connectors,
        user_id=t.user_id,
        connection_id=t.connection_id,
        now=p.clock.now(),
        owner=owner,
    )


def _state(p: Pipeline) -> tuple[Any, ...]:
    return p.rows("SELECT awaiting, needs_reply FROM conversations")[0]


async def test_trash_and_restore_follow_the_provider(isolated_db: TempDatabase) -> None:
    async with pipeline(isolated_db) as p:
        p.fake_ai.responders["EmailExtraction"] = _extraction
        t = await p.tenant()
        p.feed(t).add(_mail(t, "T-1"))
        await _sync(p, t, "1")
        await p.drain()
        assert _state(p) == ("user", True)
        assert p.scalar("SELECT count(*) FROM work_items") == 1

        p.feed(t).move_to_trash("T-1")
        await _sync(p, t, "2")
        await p.drain()
        assert p.rows("SELECT trashed, 'trash' = ANY(categories) FROM source_items") == [(True, True)]
        assert _state(p) == ("none", False)  # out of needs-response
        assert p.scalar("SELECT count(*) FROM outbox WHERE event_type = 'SourceItemTrashed'") == 1
        assert p.scalar("SELECT count(*) FROM outbox WHERE event_type = 'ConversationStateChanged'") == 1
        assert p.scalar("SELECT count(*) FROM evidence WHERE quote = %s", (BODY,)) == 1  # restorable
        assert p.scalar("SELECT archived FROM work_items") is False

        p.feed(t).restore_from_trash("T-1")
        await _sync(p, t, "3")
        await p.drain()
        assert p.rows("SELECT trashed, 'trash' = ANY(categories) FROM source_items") == [(False, False)]
        assert _state(p) == ("user", True)
        assert p.scalar("SELECT count(*) FROM outbox WHERE event_type = 'SourceItemRestored'") == 1

        p.feed(t).restore_from_trash("T-1")  # restoring a message that is not in Trash: no event
        await _sync(p, t, "4")
        await p.drain()
        assert p.scalar("SELECT count(*) FROM outbox WHERE event_type = 'SourceItemRestored'") == 1


async def test_mail_imported_in_spam_never_awaits_a_reply(isolated_db: TempDatabase) -> None:
    async with pipeline(isolated_db) as p:
        p.fake_ai.responders["EmailExtraction"] = _extraction
        t = await p.tenant()
        p.feed(t).add(_mail(t, "S-1", ("spam",)))
        await _sync(p, t, "1")
        await p.drain()
        assert p.scalar("SELECT trashed FROM source_items") is True
        assert p.rows("SELECT awaiting, needs_reply FROM conversations") in ([("none", False)], [])
