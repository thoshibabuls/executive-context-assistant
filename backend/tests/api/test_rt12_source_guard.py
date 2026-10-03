"""RT-12: extract and apply code paths never write SOURCE tables (BACKEND_DESIGN.md §6.3, §21).

A test-only trigger (installed by this fixture with the migration role, never by Alembic) guards
the SOURCE tables. When ``eca.code_path`` is ``extract`` or ``apply`` it rejects INSERT, DELETE
and TRUNCATE, and UPDATE of any column outside the allowed derived columns (the stage columns of
``source_items``, the triage projection of ``messages``). The real mail and meeting pipelines must
pass with the guard installed, and an injected write must be rejected.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from eca.connectors import NormalizedMessage, NormalizedPerson
from eca.intelligence import set_code_path
from eca.intelligence.provider.types import GenerateRequest
from eca.platform.db import create_engine, create_session_factory
from eca.platform.uow import UnitOfWorkFactory
from tests.api.support import ApiHarness, api_harness
from tests.conftest import TempDatabase

pytestmark = pytest.mark.db

STAGE_COLUMNS = (
    "stage",
    "stage_attempts",
    "stage_updated_at",
    "next_attempt_at",
    "last_error_code",
    "updated_at",
)
GUARDED: dict[str, tuple[str, ...]] = {
    "users": (),
    "connections": (),
    "sync_cursors": (),
    "source_items": STAGE_COLUMNS,
    "messages": ("triage", "triage_extraction_id", "updated_at"),
    "message_participants": (),
    "transcript_segments": (),  # Phase 4 SOURCE table: transcripts are evidence, never AI-written
}

GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION eca_test_source_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  path text := coalesce(current_setting('eca.code_path', true), '');
  col text;
BEGIN
  IF path NOT IN ('extract', 'apply') THEN
    RETURN COALESCE(NEW, OLD);
  END IF;
  IF TG_OP <> 'UPDATE' THEN
    RAISE EXCEPTION 'RT-12: % on % from the % code path', TG_OP, TG_TABLE_NAME, path;
  END IF;
  FOR col IN SELECT jsonb_object_keys(to_jsonb(NEW)) LOOP
    IF NOT (col = ANY (TG_ARGV)) AND (to_jsonb(NEW) -> col) IS DISTINCT FROM (to_jsonb(OLD) -> col) THEN
      RAISE EXCEPTION 'RT-12: update of %.% from the % code path', TG_TABLE_NAME, col, path;
    END IF;
  END LOOP;
  RETURN NEW;
END $$
"""


def _install_guard(admin_url: str) -> None:
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(GUARD_FUNCTION)
        for table, allowed in GUARDED.items():
            args = ", ".join(f"'{c}'" for c in allowed)
            conn.execute(
                f"CREATE TRIGGER rt12_guard BEFORE INSERT OR UPDATE OR DELETE ON {table} "
                f"FOR EACH ROW EXECUTE FUNCTION eca_test_source_guard({args})"
            )
            conn.execute(
                f"CREATE TRIGGER rt12_guard_truncate BEFORE TRUNCATE ON {table} "
                "FOR EACH STATEMENT EXECUTE FUNCTION eca_test_source_guard()"
            )


PROMISE = "I will send the revised budget by Friday."
VTT = (
    f"WEBVTT\n\n00:00:01.000 --> 00:00:04.000\n<v Jordan>{PROMISE}\n\n"
    "00:00:05.000 --> 00:00:08.000\n<v Avery>We decided to keep the vendor.\n"
).encode()


def _email(request: GenerateRequest) -> dict[str, Any]:
    return {
        "gist": "Budget.",
        "triage": {
            "category": "action",
            "needs_reply": True,
            "request_type": "reply",
            "business_impact": "high",
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
        "decisions": [
            {
                "kind": "open_question",
                "statement": "Which vendor?",
                "evidence_quote": PROMISE,
                "confidence": 0.6,
            }
        ],
    }


def _meeting(request: GenerateRequest) -> dict[str, Any]:
    return {
        "summary": "Budget and vendor.",
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
        "decisions": [
            {
                "kind": "decision",
                "statement": "Keep the vendor",
                "confidence": 0.9,
                "evidence": {"segment_seq": 1, "quote": "keep the vendor"},
            }
        ],
        "speaker_mapping": [
            {
                "label": "Jordan",
                "person_email": "jordan.blake@kestrel.example",
                "confidence": 0.95,
                "quote": "",
            }
        ],
    }


@pytest.fixture
def h(isolated_db: TempDatabase, tmp_path: Path) -> Iterator[ApiHarness]:
    _install_guard(isolated_db.admin_url)
    with api_harness(isolated_db, tmp_path) as harness:
        yield harness


def test_rt12_the_real_pipelines_pass_the_source_guard(h: ApiHarness) -> None:
    h.fake_ai.responders["EmailExtraction"] = _email
    h.fake_ai.responders["MeetingExtraction"] = _meeting
    avery = h.user("avery@brightwater.example", "Avery Lindqvist")
    h.connect_mail(
        avery,
        [
            NormalizedMessage(
                external_id="RT12-1",
                thread_external_id="<rt12@test.example>",
                rfc822_id="<rt12@test.example>",
                in_reply_to=None,
                sent_at=datetime.datetime(2026, 9, 21, 9, 0, tzinfo=datetime.UTC),
                sender=NormalizedPerson("jordan.blake@kestrel.example", "Jordan Blake"),
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
        json={"mime": "text/vtt", "bytes": len(VTT), "sha256": hashlib.sha256(VTT).hexdigest()},
        headers={"Idempotency-Key": "rt12"},
    )
    upload = r.json()["upload"]
    assert h.client.put(upload["url"], content=VTT, headers=upload["headers"]).status_code == 201
    assert (
        h.request(avery, "POST", f"/api/v1/recordings/{r.json()['recording']['id']}/complete").status_code
        == 202
    )
    h.drain()  # raises if any extract or apply transaction touched a guarded column

    assert h.scalar("SELECT count(*) FROM extractions WHERE apply_status = 'applied'") == 2
    assert h.scalar("SELECT count(*) FROM work_items") >= 1
    assert h.scalar("SELECT triage IS NOT NULL FROM messages") is True  # the allowed projection


async def _inject(db: TempDatabase, user_id: Any, path: str, statement: str) -> None:
    engine = create_engine(db.worker_url, pool_size=1, max_overflow=0)
    try:
        async with UnitOfWorkFactory(create_session_factory(engine))(user_id=user_id) as uow:
            await set_code_path(uow, path)
            await uow.session.execute(text(statement))
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    ("path", "statement", "rejected"),
    [
        ("apply", "UPDATE messages SET subject = 'changed'", True),
        ("extract", "UPDATE messages SET body_clean = 'changed'", True),
        ("apply", "DELETE FROM message_participants", True),
        ("apply", "UPDATE source_items SET content_hash = '\\x00'::bytea", True),
        ("apply", "UPDATE source_items SET stage_attempts = stage_attempts", False),
        ("apply", "UPDATE messages SET triage = triage", False),
        ("", "UPDATE messages SET subject = subject", False),  # outside extract/apply: not guarded
    ],
)
def test_rt12_injected_source_writes_are_rejected(
    h: ApiHarness, isolated_db: TempDatabase, path: str, statement: str, rejected: bool
) -> None:
    h.fake_ai.responders["EmailExtraction"] = _email
    avery = h.user("avery@brightwater.example", "Avery Lindqvist")
    h.connect_mail(
        avery,
        [
            NormalizedMessage(
                external_id="RT12-2",
                thread_external_id="<rt12b@test.example>",
                rfc822_id="<rt12b@test.example>",
                in_reply_to=None,
                sent_at=datetime.datetime(2026, 9, 21, 9, 0, tzinfo=datetime.UTC),
                sender=NormalizedPerson("jordan.blake@kestrel.example", "Jordan Blake"),
                to=(NormalizedPerson(avery.email, "Avery Lindqvist"),),
                cc=(),
                subject="Budget",
                body_text=PROMISE,
                body_html=None,
                categories=("inbox",),
            )
        ],
    )
    h.drain()
    run = _inject(isolated_db, avery.user_id, path or "none", statement)
    if rejected:
        with pytest.raises(DBAPIError, match="RT-12"):
            asyncio.run(run)
    else:
        asyncio.run(run)
