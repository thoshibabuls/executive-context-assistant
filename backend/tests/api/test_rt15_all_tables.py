"""RT-15 extended to every user-owned business table through Phase 4 (BACKEND_DESIGN.md §7.6, §21).

Two users each upload and process a synthetic meeting transcript through the real API and
handlers, so the Phase 1-4 tables hold rows for both. Every table with a ``<table>_user_isolation``
policy is read from the catalog (a new table is covered automatically). For each one, as the API
role and as the worker role: with ``app.user_id`` unset zero rows are visible (fail closed); with
``app.user_id`` = A exactly A's rows are visible. Moving a row to another user fails the policy's
``WITH CHECK``.
"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest

from eca.intelligence.provider.types import GenerateRequest
from tests.api.support import ApiHarness, ApiUser, api_harness
from tests.conftest import TempDatabase

pytestmark = pytest.mark.db

# Tables that must hold rows for both users after the flow below, so the checks are not vacuous.
MUST_HAVE_ROWS = {
    "source_items",
    "recordings",
    "transcript_segments",
    "meetings",
    "meeting_participants",
    "extractions",
    "work_items",
    "evidence",
    "item_evidence",
    "decisions",
    "context_events",
    "chunks",
    "idempotency_keys",
    "auth_sessions",
    "persons",
}


def _vtt(name: str) -> bytes:
    return (
        "WEBVTT\n\n"
        f"00:00:01.000 --> 00:00:05.000\n<v {name}>I will draft the {name} plan by Monday.\n\n"
        "00:00:06.000 --> 00:00:09.000\n<v Guest>We agreed to keep the scope small.\n"
    ).encode()


def _extraction(request: GenerateRequest) -> dict[str, Any]:
    transcript = " ".join(str(c) for c in request.contents)
    name = "Avery" if "Avery plan" in transcript else "Blake"
    return {
        "summary": "Planning.",
        "statements": [
            {
                "statement_kind": "promise",
                "action": "Draft the plan",
                "speaker": name,
                "owner_ref": "speaker",
                "confidence": 0.9,
                "evidence": {"segment_seq": 0, "quote": f"I will draft the {name} plan by Monday"},
            }
        ],
        "decisions": [
            {
                "kind": "decision",
                "statement": "Keep the scope small",
                "confidence": 0.9,
                "evidence": {"segment_seq": 1, "quote": "keep the scope small"},
            }
        ],
    }


def _process(h: ApiHarness, user: ApiUser, data: bytes) -> str:
    r = h.request(
        user,
        "POST",
        "/api/v1/recordings",
        json={"mime": "text/vtt", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()},
        headers={"Idempotency-Key": f"rt15-{uuid.uuid4()}"},
    )
    assert r.status_code == 201, r.text
    upload = r.json()["upload"]
    assert h.client.put(upload["url"], content=data, headers=upload["headers"]).status_code == 201
    rec_id = r.json()["recording"]["id"]
    assert h.request(user, "POST", f"/api/v1/recordings/{rec_id}/complete").status_code == 202
    h.drain()
    rec = h.request(user, "GET", f"/api/v1/recordings/{rec_id}").json()
    assert rec["status"] == "ready", rec
    label = "Avery" if b"Avery plan" in data else "Blake"
    put = h.request(
        user,
        "PUT",
        f"/api/v1/meetings/{rec['meeting_id']}/speakers",
        json={"mappings": [{"label": label, "person_id": str(user.self_person_id)}]},
    )
    assert put.status_code == 200, put.text  # creates the meeting_participants row
    h.drain()
    return str(rec["meeting_id"])


def _visible(url: str, table: str, user_id: uuid.UUID | None) -> int:
    with psycopg.connect(url) as conn:
        if user_id is not None:
            conn.execute("SELECT set_config('app.user_id', %s, true)", (str(user_id),))
        return int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])  # type: ignore[index]


@pytest.fixture
def h(isolated_db: TempDatabase, tmp_path: Path) -> Any:
    with api_harness(isolated_db, tmp_path) as harness:
        yield harness


def test_rt15_every_business_table_is_isolated_per_user(h: ApiHarness, isolated_db: TempDatabase) -> None:
    h.fake_ai.responders["MeetingExtraction"] = _extraction
    a = h.user("avery@brightwater.example", "Avery Lindqvist")
    b = h.user("blake@tallgrass.example", "Blake Ortiz")
    _process(h, a, _vtt("Avery"))
    _process(h, b, _vtt("Blake"))

    tables = sorted(
        r[0]
        for r in h.rows(
            "SELECT tablename FROM pg_policies WHERE schemaname = 'public' "
            "AND policyname = tablename || '_user_isolation' AND tablename <> 'users'"
        )
    )
    assert {"recordings", "transcript_segments", "chunks", "work_items"} <= set(tables)
    for table in tables:
        own_a = h.scalar(f"SELECT count(*) FROM {table} WHERE user_id = %s", (a.user_id,))
        own_b = h.scalar(f"SELECT count(*) FROM {table} WHERE user_id = %s", (b.user_id,))
        if table in MUST_HAVE_ROWS:
            assert own_a > 0 and own_b > 0, (table, own_a, own_b)
        for role_url in (isolated_db.runtime_url, isolated_db.worker_url):
            assert _visible(role_url, table, None) == 0, (table, "unset")
            assert _visible(role_url, table, a.user_id) == own_a, (table, "user A")
            assert _visible(role_url, table, b.user_id) == own_b, (table, "user B")


@pytest.mark.parametrize("table", ["recordings", "transcript_segments", "meetings", "chunks"])
def test_rt15_rows_cannot_be_moved_to_another_user(
    h: ApiHarness, isolated_db: TempDatabase, table: str
) -> None:
    h.fake_ai.responders["MeetingExtraction"] = _extraction
    a = h.user("avery@brightwater.example", "Avery Lindqvist")
    b = h.user("blake@tallgrass.example", "Blake Ortiz")
    _process(h, a, _vtt("Avery"))
    for role_url in (isolated_db.runtime_url, isolated_db.worker_url):
        with psycopg.connect(role_url) as conn:
            conn.execute("SELECT set_config('app.user_id', %s, true)", (str(a.user_id),))
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(f"UPDATE {table} SET user_id = %s", (b.user_id,))
    assert h.scalar(f"SELECT count(*) FROM {table} WHERE user_id = %s", (b.user_id,)) == 0
