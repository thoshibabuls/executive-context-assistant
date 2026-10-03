"""Slice 2.4 chat API (BACKEND_DESIGN.md §16.7): sessions, the SSE message route with a required
``Idempotency-Key`` and replay, the 20/minute limit, 404 on another user's session, deterministic
list answers and online grounding of AI answers (claims citing unknown packet IDs are not shown
as supported). AI calls go to the fake provider (no network). Synthetic data only.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from eca.intelligence.provider.types import GenerateRequest
from tests.api.support import ApiHarness, ApiUser, api_harness
from tests.conftest import TempDatabase

pytestmark = pytest.mark.db


@pytest.fixture
def h(isolated_db: TempDatabase, tmp_path: Path) -> Iterator[ApiHarness]:
    with api_harness(isolated_db, tmp_path) as harness:
        yield harness


def _events(text: str) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.split("\n") if ": " in line)
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def _ask(h: ApiHarness, user: ApiUser, session_id: str, text: str, key: str | None) -> Any:
    headers = {"Idempotency-Key": key} if key else {}
    return h.request(
        user, "POST", f"/api/v1/chat/sessions/{session_id}/messages", json={"text": text}, headers=headers
    )


def _session(h: ApiHarness, user: ApiUser) -> str:
    r = h.request(user, "POST", "/api/v1/chat/sessions", json={})
    assert r.status_code == 201
    return str(r.json()["id"])


def test_sessions_are_private(h: ApiHarness) -> None:
    avery = h.user("avery@brightwater.example", "Avery Lindqvist")
    blake = h.user("blake@tallgrass.example", "Blake Ortiz")
    sid = _session(h, avery)
    assert [s["id"] for s in h.request(avery, "GET", "/api/v1/chat/sessions").json()["items"]] == [sid]
    assert h.request(blake, "GET", "/api/v1/chat/sessions").json()["items"] == []
    assert h.request(blake, "GET", f"/api/v1/chat/sessions/{sid}").status_code == 404
    assert _ask(h, blake, sid, "What do I owe?", "k1").status_code == 404
    assert h.request(blake, "DELETE", f"/api/v1/chat/sessions/{sid}").status_code == 404
    assert h.request(avery, "DELETE", f"/api/v1/chat/sessions/{sid}").status_code == 204
    assert h.request(avery, "GET", f"/api/v1/chat/sessions/{sid}").status_code == 404


def test_message_needs_a_key_streams_events_and_replays(h: ApiHarness) -> None:
    avery = h.user("avery@brightwater.example", "Avery Lindqvist")
    h.request(
        avery,
        "POST",
        "/api/v1/work-items",
        json={
            "title": "Send the revised budget",
            "type": "commitment",
            "direction": "my_commitment",
            "owner_person_id": str(avery.self_person_id),
        },
    )
    sid = _session(h, avery)
    assert _ask(h, avery, sid, "What do I owe?", None).status_code == 422
    assert _ask(h, avery, sid, "   ", "blank").status_code == 422

    r = _ask(h, avery, sid, "What do I owe?", "turn-1")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = _events(r.text)
    names = [n for n, _ in events]
    assert names[0] == "plan" and names[-1] == "final" and "sources" in names
    final = events[-1][1]["message"]
    assert "Send the revised budget" in final["content"]
    cited = {c["cid"] for c in final["citations"]}
    assert all(set(cl["citations"]) <= cited for cl in final["claims"])
    calls = len(h.fake_ai.generate_calls)

    again = _ask(h, avery, sid, "What do I owe?", "turn-1")
    assert again.status_code == 200 and again.headers.get("Idempotent-Replay") == "true"
    assert _events(again.text)[-1][1]["message"]["id"] == final["id"]
    assert len(h.fake_ai.generate_calls) == calls  # a replay makes no model call
    messages = h.request(avery, "GET", f"/api/v1/chat/sessions/{sid}").json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]


def _ungrounded_answer(request: GenerateRequest) -> dict[str, Any]:
    return {
        "answerable": True,
        "answer_markdown": "Jordan promised the budget [S1]. The CFO approved it [S99].",
        "claims": [
            {"text": "Jordan promised the revised budget.", "citations": ["S1"], "kind": "source"},
            {"text": "The CFO approved the budget.", "citations": ["S99"], "kind": "source"},
            {"text": "The board will reject it.", "citations": [], "kind": "source"},
        ],
        "confidence": "high",
    }


def test_ai_claims_with_unknown_or_missing_citations_are_not_shown_as_supported(h: ApiHarness) -> None:
    h.fake_ai.responders["Answer"] = _ungrounded_answer
    avery = h.user("avery@brightwater.example", "Avery Lindqvist")
    h.request(avery, "POST", "/api/v1/work-items", json={"title": "Send the revised budget"})
    sid = _session(h, avery)
    r = _ask(h, avery, sid, "What did Jordan say about the budget?", "turn-ai")
    final = _events(r.text)[-1][1]["message"]
    supported = [c for c in final["claims"] if not c.get("flagged") and c["kind"] == "source"]
    known = {c["cid"] for c in final["citations"]}
    assert all(c["citations"] and set(c["citations"]) <= known for c in supported)
    assert "S99" not in known
    assert "The CFO approved" not in final["content"] or any(
        c.get("flagged") for c in final["claims"] if "CFO" in c["text"]
    )
    assert "The board will reject it" not in final["content"] or any(
        c.get("flagged") for c in final["claims"] if "board" in c["text"]
    )


def test_chat_is_limited_to_20_messages_per_minute(h: ApiHarness) -> None:
    avery = h.user("avery@brightwater.example", "Avery Lindqvist")
    sid = _session(h, avery)
    statuses = [_ask(h, avery, sid, f"What do I owe? ({i})", f"rate-{i}").status_code for i in range(21)]
    assert statuses[:20] == [200] * 20
    assert statuses[20] == 429
