"""RT-08: the same recording uploaded twice (BACKEND_DESIGN.md §21).

"One recording, one transcription, one meeting extraction." Covered at both levels: a second
upload init of the same media returns the existing recording (sha256 dedupe), and duplicate
delivery of every media event (``RecordingUploaded``, ``RecordingPrepared``, ``TranscriptStored``)
re-runs the handlers without a second AI-09 or AI-10 call, transcript version or extraction.
Media come from ffmpeg's built-in test sources (synthetic).
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from eca.intelligence.provider.types import GenerateRequest
from tests.api.support import ApiHarness, api_harness
from tests.conftest import TempDatabase

pytestmark = pytest.mark.db


def _transcription(request: GenerateRequest) -> dict[str, Any]:
    return {
        "segments": [{"start_ms": 0, "end_ms": 2000, "speaker": "Speaker 1", "text": "I'll book the room."}]
    }


def _extraction(request: GenerateRequest) -> dict[str, Any]:
    return {
        "summary": "Room booking.",
        "statements": [
            {
                "statement_kind": "promise",
                "action": "Book the room",
                "speaker": "Speaker 1",
                "owner_ref": "speaker",
                "confidence": 0.9,
                "evidence": {"segment_seq": 0, "quote": "I'll book the room."},
            }
        ],
    }


@pytest.fixture
def h(isolated_db: TempDatabase, tmp_path: Path) -> Iterator[ApiHarness]:
    with api_harness(isolated_db, tmp_path) as harness:
        yield harness


def test_rt08_same_recording_uploaded_twice(h: ApiHarness, tmp_path: Path) -> None:
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        pytest.skip("ffmpeg/ffprobe not installed")  # CI installs them; test_media_pipeline fails without
    wav = tmp_path / "tone.wav"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=duration=3", str(wav)],
        check=True,
    )
    data = wav.read_bytes()
    h.fake_ai.responders["Transcription"] = _transcription
    h.fake_ai.responders["MeetingExtraction"] = _extraction
    user = h.user("avery@brightwater.example", "Avery Lindqvist")
    body = {"mime": "audio/wav", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    first = h.request(user, "POST", "/api/v1/recordings", json=body, headers={"Idempotency-Key": "a"})
    assert first.status_code == 201
    upload = first.json()["upload"]
    assert h.client.put(upload["url"], content=data, headers=upload["headers"]).status_code == 201
    rec_id = first.json()["recording"]["id"]
    assert h.request(user, "POST", f"/api/v1/recordings/{rec_id}/complete").status_code == 202
    h.drain()

    second = h.request(user, "POST", "/api/v1/recordings", json=body, headers={"Idempotency-Key": "b"})
    assert second.status_code == 200 and second.json()["recording"]["id"] == rec_id
    assert second.json()["duplicate"] is True and second.json()["upload"] is None
    assert h.request(user, "POST", f"/api/v1/recordings/{rec_id}/complete").status_code == 202

    redelivered = h.redeliver(["RecordingUploaded", "RecordingPrepared", "TranscriptStored"])
    assert redelivered == 3

    assert h.scalar("SELECT count(*) FROM recordings") == 1
    assert h.scalar("SELECT status FROM recordings") == "ready"
    assert h.scalar("SELECT count(DISTINCT transcription_model) FROM transcript_segments") == 1
    assert h.scalar("SELECT count(*) FROM transcript_segments") == 1
    assert h.scalar("SELECT count(*) FROM extractions WHERE pipeline = 'meeting_extract'") == 1
    assert h.scalar("SELECT count(*) FROM meetings") == 1
    assert h.scalar("SELECT count(*) FROM work_items") == 1
    assert len(h.fake_ai.calls_for("Transcription")) == 1
    assert len(h.fake_ai.calls_for("MeetingExtraction")) == 1
    assert len(h.fake_ai.uploads) == 1
    assert h.scalar("SELECT count(*) FROM ai_calls WHERE role IN ('transcribe', 'meeting_extract')") == 2
