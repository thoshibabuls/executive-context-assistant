"""RT-10: the account deletion job crashes midway and resumes; jobs for the user are still queued
(BACKEND_DESIGN.md §13.3, §21).

"Deletion completes; queued jobs no-op; no rows remain for the user except the content-free audit
record." The user has a processed meeting (recording, raw and prepared objects, transcript,
chunks, items, decisions, prep), an upload whose processing is still queued, and a provider file
kept after a failed transcription. Every table with a ``user_id`` column is read from the catalog,
so a new table is covered automatically. All data are synthetic.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import eca.privacy.service as privacy_service
from eca.intelligence.provider.types import GenerateRequest
from eca.platform.uow import UnitOfWork
from tests.api.support import ApiHarness, ApiUser, api_harness
from tests.conftest import TempDatabase

pytestmark = pytest.mark.db

VTT = (
    b"WEBVTT\n\n"
    b"00:00:01.000 --> 00:00:05.000\n<v Avery>I will review the contract by Thursday.\n\n"
    b"00:00:06.000 --> 00:00:09.000\n<v Jordan>We agreed to renew for one year.\n"
)


def _extraction(request: GenerateRequest) -> dict[str, Any]:
    return {
        "summary": "Contract renewal.",
        "statements": [
            {
                "statement_kind": "promise",
                "action": "Review the contract",
                "speaker": "Avery",
                "owner_ref": "speaker",
                "confidence": 0.9,
                "evidence": {"segment_seq": 0, "quote": "I will review the contract by Thursday"},
            }
        ],
        "decisions": [
            {
                "kind": "decision",
                "statement": "Renew for one year",
                "confidence": 0.9,
                "evidence": {"segment_seq": 1, "quote": "renew for one year"},
            }
        ],
    }


def _upload(h: ApiHarness, user: ApiUser, data: bytes, mime: str = "text/vtt") -> str:
    r = h.request(
        user,
        "POST",
        "/api/v1/recordings",
        json={"mime": mime, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()},
        headers={"Idempotency-Key": f"rt10-{uuid.uuid4()}"},
    )
    assert r.status_code == 201, r.text
    upload = r.json()["upload"]
    assert h.client.put(upload["url"], content=data, headers=upload["headers"]).status_code == 201
    rec_id = str(r.json()["recording"]["id"])
    assert h.request(user, "POST", f"/api/v1/recordings/{rec_id}/complete").status_code == 202
    return rec_id


def _user_tables(h: ApiHarness) -> list[str]:
    return sorted(
        r[0]
        for r in h.rows(
            "SELECT c.table_name FROM information_schema.columns c JOIN pg_tables t "
            "ON t.tablename = c.table_name AND t.schemaname = 'public' "
            "WHERE c.table_schema = 'public' AND c.column_name = 'user_id'"
        )
    )


def _rows_of(h: ApiHarness, user_id: uuid.UUID) -> dict[str, int]:
    counts = {
        t: h.scalar(f"SELECT count(*) FROM {t} WHERE user_id = %s", (user_id,)) for t in _user_tables(h)
    }
    counts["users"] = h.scalar("SELECT count(*) FROM users WHERE id = %s", (user_id,))
    return {t: n for t, n in counts.items() if n}


@pytest.fixture
def h(isolated_db: TempDatabase, tmp_path: Path) -> Iterator[ApiHarness]:
    with api_harness(isolated_db, tmp_path) as harness:
        yield harness


def test_rt10_deletion_crashes_midway_resumes_and_leaves_nothing(
    h: ApiHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h.fake_ai.responders["MeetingExtraction"] = _extraction
    avery = h.user("avery@brightwater.example", "Avery Lindqvist")
    blake = h.user("blake@tallgrass.example", "Blake Ortiz")  # must be untouched
    rec_id = _upload(h, avery, VTT)
    _upload(h, blake, VTT)
    h.drain()
    meeting_id = h.request(avery, "GET", f"/api/v1/recordings/{rec_id}").json()["meeting_id"]
    h.request(
        avery,
        "PUT",
        f"/api/v1/meetings/{meeting_id}/speakers",
        json={"mappings": [{"label": "Avery", "person_id": str(avery.self_person_id)}]},
    )
    h.drain()
    # A provider file reference kept after a failed transcription (it must be deleted too).
    ref = {"name": "files/kept", "uri": "fake://kept", "mime_type": "audio/ogg", "sha256": "0" * 64}
    h.rows(
        "UPDATE recordings SET provider_file_ref = %s::jsonb, "
        "provider_file_expires_at = now() + interval '1 hour' "
        "WHERE id = %s RETURNING id",
        (json.dumps(ref), rec_id),
    )
    # Queued work for the user: an upload completed but not processed yet.
    queued_rec = _upload(h, avery, b"WEBVTT\n\n00:01.000 --> 00:02.000\n<v Avery>Queued.")
    queued = h.rows(
        "SELECT id, user_id, event_type, aggregate_type, aggregate_id, payload, correlation, created_at "
        "FROM outbox WHERE status = 'pending' AND aggregate_id = %s",
        (queued_rec,),
    )
    assert len(queued) == 1

    before = _rows_of(h, avery.user_id)
    assert {"recordings", "transcript_segments", "chunks", "work_items", "decisions", "meetings"} <= set(
        before
    )
    blake_before = _rows_of(h, blake.user_id)
    objects_before = sorted(
        p.name for p in h.objects.rglob("*") if p.is_file() and str(avery.user_id) in str(p)
    )
    assert objects_before

    r = h.request(avery, "DELETE", "/api/v1/me")
    assert r.status_code == 202, r.text
    assert h.scalar("SELECT status FROM users WHERE id = %s", (avery.user_id,)) == "deleting"
    assert h.request(avery, "GET", "/api/v1/recordings").status_code == 401  # sessions revoked

    # Crash in the middle: the "meetings" step fails once, after earlier steps committed.
    original = dict(privacy_service._ACCOUNT_STEPS)

    async def crash(uow: UnitOfWork) -> None:
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(
        privacy_service,
        "_ACCOUNT_STEPS",
        tuple((name, crash if name == "meetings" else step) for name, step in original.items()),
    )
    with pytest.raises(RuntimeError, match="simulated crash"):
        h.drain()
    progress = h.scalar("SELECT progress FROM deletion_jobs WHERE user_id = %s", (avery.user_id,))
    assert {"objects", "tokens", "chat", "work", "communication"} <= set(progress["done"])
    assert "meetings" not in progress["done"]
    assert h.scalar("SELECT count(*) FROM users WHERE id = %s", (avery.user_id,)) == 1

    # Resume: the job is delivered again (handler retry) and completes from the failed step.
    monkeypatch.setattr(privacy_service, "_ACCOUNT_STEPS", tuple(original.items()))
    assert h.redeliver(["UserDeletionRequested"]) == 1
    # The queued job runs after the deletion: it must do nothing.
    h.redeliver_rows(queued)

    assert _rows_of(h, avery.user_id) == {}
    assert not [p for p in h.objects.rglob("*") if p.is_file() and str(avery.user_id) in str(p)]
    assert "files/kept" in h.fake_ai.deleted
    audit = h.rows("SELECT action, user_id, actor FROM audit_log WHERE action = 'account_deleted'")
    assert audit == [("account_deleted", None, "system")]
    assert h.scalar("SELECT count(*) FROM ai_calls WHERE user_id = %s", (avery.user_id,)) == 0
    assert _rows_of(h, blake.user_id) == blake_before
    assert h.request(blake, "GET", "/api/v1/recordings").status_code == 200
