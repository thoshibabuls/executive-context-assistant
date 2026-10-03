"""CC-01 to CC-10 at L2 through the real pipeline (IMPLEMENTATION_PLAN.md slice 1.4 and Phase 1
exit; CONTEXT_EVALUATION.md §6).

Each chain's sources go through the production code: emails through the fake mail connector,
meeting transcripts as calendar meetings with an uploaded WebVTT file, user actions through the
API. AI-01 and AI-10 answers are **placeholders** (hand-authored from the chain labels, as
IMPLEMENTATION_PLAN.md Batch A allows): they test the deterministic pipeline (apply, mapping,
dates, matching and merge, status fold, conflicts), never model quality. After the last source of
each checkpoint the expected item state is compared field by field. Synthetic data only.
"""

from __future__ import annotations

import datetime
import hashlib
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import yaml

from eca.connectors import NormalizedAttendee, NormalizedEvent, NormalizedMessage, NormalizedPerson
from eca.intelligence.provider.types import GenerateRequest
from eca_evals.context.chains import Chain, ExpectedItem, Source, load_chains
from tests.api.support import ApiHarness, ApiUser, api_harness
from tests.conftest import TempDatabase

pytestmark = pytest.mark.db

REPO = Path(__file__).resolve().parents[3]
CHAINS = load_chains(REPO / "evals" / "context" / "chains")
PEOPLE = {
    p["id"]: p
    for p in yaml.safe_load(
        (REPO / "evals" / "ai" / "datasets" / "world_v1" / "scenario.yaml").read_text("utf-8")
    )["people"]
}
REPORTED = {
    "in_progress": "in progress",
    "completed_claim": "claims done",
    "delayed": "delayed",
    "withdrawn": "withdrawn",
}
TRIAGE = {
    "category": "action",
    "needs_reply": False,
    "request_type": "none",
    "business_impact": "medium",
    "confidence": 0.9,
}

# Item key → a title fragment that identifies it (the placeholder answers use these titles).
KEYS = {
    "wi_secdoc": "security documentation",
    "wi_pentest": "pen-test report",
    "wi_vendorrisk": "vendor risk report",
    "wi_runbook": "migration runbook",
    "wi_loadtest": "load-test results",
    "wi_dpa": "data processing addendum",
    "wi_bridge": "SOC 2 bridge letter",
    "wi_pricing": "pricing sheet",
    "wi_secpack": "security pack",
    "wi_redlines": "contract redlines",
    "wi_cutover": "API cutover plan",
}


def _promise(action: str, quote: str, due: str | None, **extra: Any) -> dict[str, Any]:
    return {
        "statement_kind": "promise",
        "action": action,
        "owner_ref": "speaker",
        "due_text": due,
        "confidence": 0.92,
        "evidence_quote": quote,
        **extra,
    }


def _signal(keyword: str, signal: str, quote: str, new_due: str | None = None) -> dict[str, Any]:
    return {
        "candidate": keyword,
        "signal": signal,
        "evidence_quote": quote,
        "new_due_text": new_due,
        "confidence": 0.9,
    }


# Placeholder AI answers per chain and source: statements, status signals (by candidate keyword)
# and, for transcripts, speaker proposals.
ANSWERS: dict[str, dict[str, dict[str, Any]]] = {
    "CC-01": {
        "m1": {
            "statements": [
                {
                    **_promise(
                        "Send the security documentation",
                        "We'll send you the security documentation by October 8.",
                        "by October 8",
                    ),
                    "speaker": "John Pellegrini",
                }
            ],
            "speakers": {"John Pellegrini": "p_john_hv"},
        },
        "e1": {
            "statements": [
                {
                    "statement_kind": "report_request",
                    "action": "Update on the security documentation",
                    "owner_ref": "recipient:john.pellegrini@halvorsen.example",
                    "candidate": "security documentation",
                    "confidence": 0.85,
                    "evidence_quote": "Following up on the documentation from Monday's meeting. Any update?",
                }
            ]
        },
        "e2": {
            "signals": [
                _signal(
                    "security documentation",
                    "progress",
                    "Still working on the security docs, should be on track.",
                )
            ]
        },
    },
    "CC-02": {
        "e1": {
            "statements": [
                _promise(
                    "Send the pen-test report",
                    "I will send the pen-test report by October 8.",
                    "by October 8",
                )
            ]
        },
        "e2": {
            "signals": [
                _signal(
                    "pen-test report",
                    "new_deadline",
                    "Sorry, I need until the 10th to finish the pen-test report.",
                    new_due="until the 10th",
                )
            ]
        },
    },
    "CC-03": {
        "e1": {
            "statements": [
                _promise(
                    "Send the vendor risk report",
                    "I will send the vendor risk report by October 9.",
                    "by October 9",
                )
            ]
        },
        "e2": {"signals": [_signal("vendor risk report", "delay", "John's vendor risk report will be late")]},
    },
    "CC-04": {
        "e1": {
            "statements": [
                _promise(
                    "Send the migration runbook", "I will send the migration runbook by Friday.", "by Friday"
                )
            ]
        },
        "e2": {
            "signals": [
                _signal("migration runbook", "completed_claim", "I've attached the migration runbook")
            ]
        },
    },
    "CC-05": {
        "e1": {
            "statements": [
                _promise(
                    "Send the load-test results",
                    "I will send the load-test results by October 1.",
                    "by October 1",
                )
            ]
        },
        "e2": {
            "signals": [_signal("load-test results", "completed_claim", "Sending the load-test results now.")]
        },
    },
    "CC-06": {
        "m1": {
            "statements": [
                {
                    **_promise(
                        "Send the data processing addendum",
                        "I'll send the data processing addendum by October 6.",
                        "by October 6",
                    ),
                    "speaker": "Mei Lin",
                }
            ],
            "speakers": {"Mei Lin": "p_mei"},
        },
        "e1": {
            "statements": [
                _promise(
                    "Send the data processing addendum",
                    "I'll send the data processing addendum by October 6.",
                    "by October 6",
                )
            ]
        },
    },
    "CC-07": {
        "e1": {
            "statements": [
                _promise(
                    "Send the SOC 2 bridge letter",
                    "I will send the SOC 2 bridge letter by October 5",
                    "by October 5",
                ),
                _promise(
                    "Send the pricing sheet", "I will send the pricing sheet by October 7", "by October 7"
                ),
            ]
        },
        "e2": {"signals": [_signal("pricing sheet", "delay", "the pricing sheet will be a few days late")]},
    },
    "CC-08": {
        "e1": {
            "statements": [
                _promise(
                    "Send the completed security pack",
                    "I will send the completed security pack by October 9.",
                    "by October 9",
                )
            ]
        },
        "e2": {
            "statements": [
                _promise(
                    "Send the final security pack",
                    "The final version follows by October 9.",
                    "by October 9",
                    candidate="security pack",
                )
            ]
        },
    },
    "CC-09": {
        "e1": {
            "statements": [
                _promise(
                    "Send the contract redlines",
                    "I will send the contract redlines by October 7.",
                    "by October 7",
                )
            ]
        },
        "e2": {
            "signals": [_signal("contract redlines", "cancelled", "we won't be able to provide the redlines")]
        },
    },
    "CC-10": {
        "e1": {
            "statements": [
                _promise(
                    "Deliver the API cutover plan",
                    "We will deliver the API cutover plan by October 12.",
                    "by October 12",
                )
            ]
        },
        "e2": {
            "signals": [
                _signal(
                    "API cutover plan",
                    "new_deadline",
                    "Our team will deliver the API cutover plan by October 9.",
                    new_due="by October 9",
                )
            ]
        },
    },
}


def _person(pid: str) -> NormalizedPerson:
    return NormalizedPerson(PEOPLE[pid]["email"], PEOPLE[pid]["name"])


def _candidates(prompt: str) -> dict[str, str]:
    return dict(re.findall(r"^- (C\d+): (.*)$", prompt, re.MULTILINE))


def _code(prompt: str, keyword: str) -> str:
    for code, summary in _candidates(prompt).items():
        if keyword.lower() in summary.lower():
            return code
    raise AssertionError(f"no candidate for {keyword!r} in the prompt")


@dataclass
class ChainRun:
    chain: Chain
    h: ApiHarness
    user: ApiUser
    source_items: dict[str, Any] = field(default_factory=dict)  # chain source id → source item ID

    def answer(self, source: Source, request: GenerateRequest) -> dict[str, Any]:
        spec = ANSWERS[self.chain.id].get(source.id, {})
        prompt = str(request.contents[0])
        out: dict[str, Any]
        if source.kind == "email":
            statements = []
            for st in spec.get("statements", []):
                st = dict(st)
                keyword = st.pop("candidate", None)
                if keyword:
                    st["candidate_id"] = _code(prompt, keyword)
                statements.append(st)
            signals = []
            for sig in spec.get("signals", []):
                sig = dict(sig)
                sig["candidate_id"] = _code(prompt, sig.pop("candidate"))
                signals.append({k: v for k, v in sig.items() if v is not None})
            out = {
                "gist": source.subject or "",
                "triage": TRIAGE,
                "statements": statements,
                "status_signals": signals,
            }
        else:
            statements = []
            for st in spec.get("statements", []):
                st = dict(st)
                quote = st.pop("evidence_quote")
                statements.append({**st, "evidence": {"segment_seq": 0, "quote": quote}})
            out = {
                "summary": "",
                "statements": statements,
                "speaker_mapping": [
                    {"label": label, "person_email": PEOPLE[pid]["email"], "confidence": 0.95, "quote": ""}
                    for label, pid in spec.get("speakers", {}).items()
                ],
            }
        return out


def _mail(run: ChainRun, src: Source, thread_roots: dict[str, str]) -> NormalizedMessage:
    root_subject = re.sub(r"^(?:re|fwd?):\s*", "", src.subject or "", flags=re.I).strip()
    thread = thread_roots.setdefault(root_subject, f"<{run.chain.id}-{src.id}@chains.example>")
    sender = src.sender or "p_user"
    return NormalizedMessage(
        external_id=f"{run.chain.id}-{src.id}",
        thread_external_id=thread,
        rfc822_id=f"<{run.chain.id}-{src.id}@chains.example>",
        in_reply_to=thread if thread != f"<{run.chain.id}-{src.id}@chains.example>" else None,
        sent_at=src.occurred_at.astimezone(datetime.UTC),
        sender=_person(sender),
        to=tuple(_person(p) for p in src.to),
        cc=(),
        subject=src.subject,
        body_text=src.body,
        body_html=None,
        categories=("sent",) if sender == "p_user" else ("inbox",),
    )


def _meeting(run: ChainRun, src: Source) -> None:
    start = src.occurred_at.astimezone(datetime.UTC)
    run.h.sync_calendar(
        run.user,
        [
            NormalizedEvent(
                external_id=f"{run.chain.id}-{src.id}",
                series_external_id=None,
                ical_uid=f"{run.chain.id}-{src.id}@chains.example",
                start=start,
                end=start + datetime.timedelta(hours=1),
                timezone="America/Los_Angeles",
                title=f"{run.chain.id} meeting",
                description=None,
                attendees=tuple(
                    NormalizedAttendee(PEOPLE[p]["email"], PEOPLE[p]["name"], "accepted")
                    for p in src.attendees
                ),
                organizer=_person("p_user"),
                conference_uri=None,
                status="confirmed",
            )
        ],
    )
    run.h.drain()
    meeting_id = run.h.scalar("SELECT id FROM meetings WHERE title = %s", (f"{run.chain.id} meeting",))
    vtt = (src.transcript or "").replace("\n\n", "\n\n").encode()
    r = run.h.request(
        run.user,
        "POST",
        "/api/v1/recordings",
        json={
            "mime": "text/vtt",
            "bytes": len(vtt),
            "sha256": hashlib.sha256(vtt).hexdigest(),
            "meeting_id": str(meeting_id),
            "occurred_at": start.isoformat(),
        },
        headers={"Idempotency-Key": f"{run.chain.id}-{src.id}"},
    )
    assert r.status_code == 201, r.text
    upload = r.json()["upload"]
    assert run.h.client.put(upload["url"], content=vtt, headers=upload["headers"]).status_code == 201
    rec_id = r.json()["recording"]["id"]
    assert run.h.request(run.user, "POST", f"/api/v1/recordings/{rec_id}/complete").status_code == 202
    run.source_items[src.id] = run.h.scalar("SELECT source_item_id FROM recordings WHERE id = %s", (rec_id,))


def _items_matching(run: ChainRun, key: str) -> list[tuple[Any, ...]]:
    return run.h.rows(
        "SELECT id, type, owner_person_id, counterparty_person_id, direction, due_at, lifecycle_status, "
        "reported_status, has_conflict FROM work_items WHERE title ILIKE %s AND merged_into_id IS NULL",
        (f"%{KEYS[key]}%",),
    )


def _person_id(run: ChainRun, pid: str) -> Any:
    return run.h.scalar("SELECT id FROM persons WHERE primary_email = %s", (PEOPLE[pid]["email"],))


def _check(run: ChainRun, expected: ExpectedItem, at: datetime.datetime) -> list[str]:
    problems: list[str] = []
    rows = _items_matching(run, expected.key)
    if expected.item_count_matching is not None and len(rows) != expected.item_count_matching:
        problems.append(f"{expected.key}: {len(rows)} items, expected {expected.item_count_matching}")
    if not rows:
        return [*problems, f"{expected.key}: missing"]
    item_id, type_, owner, counterparty, direction, due_at, lifecycle, reported, conflict = rows[0]
    tz = ZoneInfo(run.chain.user.get("timezone", "UTC"))

    def want(name: str, actual: Any, wanted: Any) -> None:
        if actual != wanted:
            problems.append(f"{expected.key}.{name}: {actual!r} != {wanted!r}")

    if expected.type:
        want("type", type_, expected.type)
    if expected.owner:
        want("owner", owner, _person_id(run, expected.owner))
    if expected.counterparty:
        want("counterparty", counterparty, _person_id(run, expected.counterparty))
    if expected.direction:
        want("direction", direction, expected.direction)
    if expected.due:
        want("due", due_at.astimezone(tz).date() if due_at else None, expected.due)
    if expected.lifecycle:
        want("lifecycle", lifecycle, expected.lifecycle)
    if "reported_status" in expected.model_fields_set:
        want(
            "reported_status",
            reported,
            REPORTED.get(expected.reported_status or "", expected.reported_status),
        )
    if expected.has_conflict is not None:
        want("has_conflict", conflict, expected.has_conflict)
    if expected.evidence:
        got = {
            r[0]
            for r in run.h.rows(
                "SELECT e.source_item_id FROM item_evidence ie JOIN evidence e ON e.id = ie.evidence_id "
                "WHERE ie.item_type = 'work_item' AND ie.item_id = %s",
                (item_id,),
            )
        }
        wanted = {run.source_items[s] for s in expected.evidence if s in run.source_items}
        want("evidence", sorted(map(str, got)), sorted(map(str, wanted)))
    return problems


@pytest.fixture
def h(isolated_db: TempDatabase, tmp_path: Path) -> Iterator[ApiHarness]:
    with api_harness(isolated_db, tmp_path) as harness:
        yield harness


@pytest.mark.parametrize("chain", CHAINS, ids=[c.id for c in CHAINS])
def test_chain_at_l2(h: ApiHarness, chain: Chain) -> None:
    user = h.user(PEOPLE["p_user"]["email"], PEOPLE["p_user"]["name"], chain.user.get("timezone", "UTC"))
    run = ChainRun(chain, h, user)
    current: dict[str, Source] = {}

    def responder(kind: str) -> Callable[[GenerateRequest], dict[str, Any]]:
        def answer(request: GenerateRequest) -> dict[str, Any]:
            prompt = str(request.contents[0])
            for src in chain.sources:
                if src.kind == kind and src.text.strip().splitlines()[-1].split(">", 1)[-1].strip() in prompt:
                    return run.answer(src, request)
            raise AssertionError(f"{chain.id}: no placeholder answer for this {kind} prompt")

        return answer

    h.fake_ai.responders["EmailExtraction"] = responder("email")
    h.fake_ai.responders["MeetingExtraction"] = responder("meeting_transcript")
    h.connect_mail(user, [])
    thread_roots: dict[str, str] = {}
    ordered = sorted(chain.sources, key=lambda s: s.occurred_at)
    problems: list[str] = []
    for checkpoint in sorted(chain.expected_state, key=lambda c: c.at):
        for src in [s for s in ordered if s.occurred_at <= checkpoint.at and s.id not in current]:
            current[src.id] = src
            if src.kind == "email":
                h.accounts.mail_feed(user.email).add(_mail(run, src, thread_roots))
                h.sync_mail_again(user)
                h.drain()
                run.source_items[src.id] = h.scalar(
                    "SELECT id FROM source_items WHERE external_id = %s", (f"{chain.id}-{src.id}",)
                )
            elif src.kind == "meeting_transcript":
                _meeting(run, src)
                h.drain()
            else:
                rows = _items_matching(run, src.target or "")
                assert rows, f"{chain.id}: no item for {src.target}"
                command = {"mark_done": "complete", "reject": "reject"}[src.action or ""]
                r = h.request(user, "POST", f"/api/v1/work-items/{rows[0][0]}/{command}")
                assert r.status_code == 200, r.text
                h.drain()
        for expected in checkpoint.items:
            problems += [f"{checkpoint.at:%Y-%m-%d %H:%M} {p}" for p in _check(run, expected, checkpoint.at)]
    assert not problems, f"{chain.id}:\n" + "\n".join(problems)
