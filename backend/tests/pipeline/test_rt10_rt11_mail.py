"""RT-10 and RT-11 on the mail pipeline (BACKEND_DESIGN.md §13.3, §21). Synthetic data only.

RT-11: the provider deletes a message that supports a confirmed and a suggested item. "Body and
chunks deleted; quotes redacted; suggested item archived; confirmed item ``has_source_gap``; no
dangling FK."

RT-10 (mail tables; the Phase 4 tables are covered in tests/api/test_rt10_account_deletion.py):
the account deletion job crashes midway and resumes; a job for the user still queued no-ops; no
row of the user remains except the content-free audit record; another user is untouched.
"""

from __future__ import annotations

import datetime
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text

import eca.privacy.service as privacy_service
from eca.connectors import NormalizedMessage, NormalizedPerson
from eca.identity import request_account_deletion
from eca.ingestion import sync_mail
from eca.intelligence.provider.types import GenerateRequest
from eca.platform.events import EventEnvelope
from eca.platform.uow import UnitOfWork
from eca.work.service import confirm
from tests.conftest import TempDatabase
from tests.pipeline.support import Pipeline, Tenant, pipeline, world_v1_messages

pytestmark = pytest.mark.db

BODY = "Could you send the Q3 numbers by Friday? I will share the board deck tomorrow."
RAJ = NormalizedPerson("raj.menon@kestrelbank.example", "Raj Menon")


def _message(t: Tenant) -> NormalizedMessage:
    return NormalizedMessage(
        external_id="RT11-1",
        thread_external_id="<rt11@test.example>",
        rfc822_id="<rt11@test.example>",
        in_reply_to=None,
        sent_at=datetime.datetime(2026, 9, 21, 9, 0, tzinfo=datetime.UTC),
        sender=RAJ,
        to=(NormalizedPerson(t.email, "Avery Lindqvist"),),
        cc=(),
        subject="Q3",
        body_text=BODY,
        body_html=None,
        categories=("inbox",),
    )


def _two_items(request: GenerateRequest) -> dict[str, Any]:
    statement = {
        "speaker": "sender",
        "confidence": 0.9,
        "in_forwarded_content": False,
        "is_deadline_only": False,
    }
    return {
        "gist": "Asks for numbers; promises the deck.",
        "triage": {
            "category": "action",
            "needs_reply": True,
            "request_type": "provide_info",
            "business_impact": "medium",
            "confidence": 0.9,
        },
        "statements": [
            {
                **statement,
                "statement_kind": "request",
                "action": "Send the Q3 numbers",
                "owner_ref": "recipient:" + "avery@brightwater.example",
                "due_text": "by Friday",
                "evidence_quote": "Could you send the Q3 numbers by Friday?",
            },
            {
                **statement,
                "statement_kind": "promise",
                "action": "Share the board deck",
                "owner_ref": "speaker",
                "due_text": "tomorrow",
                "evidence_quote": "I will share the board deck tomorrow.",
            },
        ],
    }


async def _sync(p: Pipeline, t: Tenant, owner: str) -> None:
    await sync_mail(
        p.worker,
        p.connectors,
        user_id=t.user_id,
        connection_id=t.connection_id,
        now=p.clock.now(),
        owner=owner,
    )


async def test_rt11_provider_deletion_of_a_message_with_a_confirmed_and_a_suggested_item(
    isolated_db: TempDatabase,
) -> None:
    async with pipeline(isolated_db) as p:
        p.fake_ai.responders["EmailExtraction"] = _two_items
        t = await p.tenant()
        p.feed(t).add(_message(t))
        await _sync(p, t, "first")
        await p.drain()
        items = dict(p.rows("SELECT title, id FROM work_items"))
        assert set(items) == {"Send the Q3 numbers", "Share the board deck"}
        assert p.scalar("SELECT count(*) FROM chunks") >= 1
        async with p.api(user_id=t.user_id) as uow:
            await confirm(uow, items["Send the Q3 numbers"], request_key="rt11-confirm", at=p.clock.now())
        await p.drain()

        p.feed(t).delete("RT11-1")
        await _sync(p, t, "second")
        await p.drain()

        assert p.rows("SELECT deleted_at IS NOT NULL FROM source_items") == [(True,)]
        assert p.rows("SELECT body_text, body_clean, body_purged_at IS NOT NULL FROM messages") == [
            (None, None, True)
        ]
        assert p.scalar("SELECT count(*) FROM chunks") == 0
        assert {q for (q,) in p.rows("SELECT quote FROM evidence")} == {"[source deleted]"}
        state = {
            title: (archived, gap)
            for title, archived, gap in p.rows("SELECT title, archived, has_source_gap FROM work_items")
        }
        assert state == {"Send the Q3 numbers": (False, True), "Share the board deck": (True, False)}
        assert p.scalar("SELECT count(*) FROM context_events WHERE event_type = 'source_removed'") == 2
        # No dangling references (FKs exist, and every evidence row still has its source row).
        assert (
            p.scalar(
                "SELECT count(*) FROM evidence e LEFT JOIN source_items s ON s.id = e.source_item_id "
                "WHERE s.id IS NULL"
            )
            == 0
        )
        assert (
            p.scalar(
                "SELECT count(*) FROM item_evidence ie LEFT JOIN evidence e ON e.id = ie.evidence_id "
                "WHERE e.id IS NULL"
            )
            == 0
        )
        assert BODY not in str(p.rows("SELECT * FROM messages")) + str(p.rows("SELECT * FROM evidence"))


def _user_tables(p: Pipeline) -> list[str]:
    return sorted(
        r[0]
        for r in p.rows(
            "SELECT c.table_name FROM information_schema.columns c JOIN pg_tables t "
            "ON t.tablename = c.table_name AND t.schemaname = 'public' "
            "WHERE c.table_schema = 'public' AND c.column_name = 'user_id'"
        )
    )


def _rows_of(p: Pipeline, user_id: UUID) -> dict[str, int]:
    counts = {
        t: p.scalar(f"SELECT count(*) FROM {t} WHERE user_id = %s", (user_id,)) for t in _user_tables(p)
    }
    counts["users"] = p.scalar("SELECT count(*) FROM users WHERE id = %s", (user_id,))
    return {t: n for t, n in counts.items() if n}


async def _run_rows(p: Pipeline, rows: list[Any]) -> None:
    executor = p.executor()
    for row in rows:
        await executor.run_envelope(EventEnvelope.model_validate(dict(row)).model_dump(mode="json"))


async def _outbox(p: Pipeline, where: str, params: dict[str, Any]) -> list[Any]:
    async with p.worker(user_id=None) as uow:
        result = await uow.session.execute(
            text(
                "SELECT id, user_id, event_type, aggregate_type, aggregate_id, payload, correlation, "
                f"created_at FROM outbox WHERE {where} ORDER BY created_at, id"
            ),
            params,
        )
        return list(result.mappings().all())


async def test_rt10_account_deletion_of_mail_data_resumes_after_a_crash(
    isolated_db: TempDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with pipeline(isolated_db) as p:
        a = await p.tenant()
        b = await p.tenant(email="blake@tallgrass.example", name="Blake Ortiz")
        # Statements ground only in the RT-11 message; the world_v1 mail adds the other tables.
        p.fake_ai.responders["EmailExtraction"] = _two_items
        p.feed(a).extend([*world_v1_messages()[:12], _message(a)])
        p.feed(b).extend(world_v1_messages()[12:16])
        await _sync(p, a, "a")
        await _sync(p, b, "b")
        await p.drain()
        before = _rows_of(p, a.user_id)
        assert {"messages", "conversations", "work_items", "chunks", "persons", "source_items"} <= set(before)
        blake_before = _rows_of(p, b.user_id)

        # A queued job for the user: a sync trigger dispatched to the queue but not yet run.
        p.feed(a).extend(world_v1_messages()[12:14])
        await _sync(p, a, "queued")
        queued = await _outbox(p, "status = 'pending' AND user_id = :u", {"u": a.user_id})
        assert queued
        async with p.worker(user_id=None) as uow:
            await uow.session.execute(
                text("UPDATE outbox SET status = 'dispatched' WHERE id = ANY(:ids)"),
                {"ids": [r["id"] for r in queued]},
            )

        async with p.api(user_id=a.user_id) as uow:
            await request_account_deletion(uow)

        original = list(privacy_service._ACCOUNT_STEPS)

        async def crash(uow: UnitOfWork) -> None:
            raise RuntimeError("simulated crash")

        monkeypatch.setattr(
            privacy_service,
            "_ACCOUNT_STEPS",
            tuple((n, crash if n == "people" else s) for n, s in original),
        )
        with pytest.raises(RuntimeError, match="simulated crash"):
            await p.drain()
        done = p.scalar("SELECT progress FROM deletion_jobs WHERE user_id = %s", (a.user_id,))["done"]
        assert {"work", "communication", "meetings", "intelligence"} <= set(done) and "people" not in done

        monkeypatch.setattr(privacy_service, "_ACCOUNT_STEPS", tuple(original))
        deletion = await _outbox(p, "event_type = 'UserDeletionRequested'", {})
        await _run_rows(p, deletion)  # the handler's retry resumes the job
        await _run_rows(p, queued)  # jobs queued before the deletion run afterwards and no-op
        await p.drain()

        assert _rows_of(p, a.user_id) == {}
        assert p.rows("SELECT user_id, actor FROM audit_log WHERE action = 'account_deleted'") == [
            (None, "system")
        ]
        assert _rows_of(p, b.user_id) == blake_before
