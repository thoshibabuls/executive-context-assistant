"""Phase 2 pure functions (IMPLEMENTATION_PLAN.md §0.6 deferred unit tests): the temporal resolver
(CONTEXT_ARCHITECTURE.md §7.1), planner rules, full-text terms, discovery diversity, ranking and
packet budgets (§9.2, §9.6, §9.10), and chunking of email and calendar text (§9.9).
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest

from eca.retrieval.chunking import MAX_TOKENS, email_chunks, estimate_tokens, split_text
from eca.retrieval.packet import Budget, PacketItem, pack
from eca.retrieval.planner import candidate_names, plan_by_rules
from eca.retrieval.ranking import authority_class, recency_decay, score
from eca.retrieval.search import diversify, ts_terms
from eca.retrieval.temporal import (
    extract_expression,
    from_checkpoint,
    resolve,
    since,
)

LA = "America/Los_Angeles"
# Wednesday 30 Sep 2026, 10:00 in Los Angeles (17:00 UTC).
NOW = datetime.datetime(2026, 9, 30, 17, 0, tzinfo=datetime.UTC)


def _utc(y: int, m: int, d: int, h: int = 0) -> datetime.datetime:
    return datetime.datetime(y, m, d, h, tzinfo=datetime.UTC)


# ---------------------------------------------------------------- temporal semantics (§7.1)


def test_today_yesterday_and_local_days() -> None:
    today = resolve("today", NOW, LA)
    assert today is not None and (today.start, today.end) == (_utc(2026, 9, 30, 7), _utc(2026, 10, 1, 7))
    y = resolve("yesterday", NOW, LA)
    assert y is not None and (y.start, y.end) == (_utc(2026, 9, 29, 7), _utc(2026, 9, 30, 7))
    assert not y.ambiguous
    early = resolve("yesterday", _utc(2026, 9, 30, 9), LA)  # 02:00 local
    assert early is not None and early.ambiguous and early.note


def test_this_week_is_monday_to_sunday_and_last_week_the_one_before() -> None:
    week = resolve("this week", NOW, LA)
    assert week is not None and (week.start, week.end) == (_utc(2026, 9, 28, 7), _utc(2026, 10, 5, 7))
    last = resolve("last week", NOW, LA)
    assert last is not None and (last.start, last.end) == (_utc(2026, 9, 21, 7), _utc(2026, 9, 28, 7))


def test_rolling_windows_and_weekdays() -> None:
    recent = resolve("recently", NOW, LA)
    assert recent is not None and recent.end - recent.start == datetime.timedelta(days=7)
    three = resolve("last three days", NOW, LA)
    assert three is not None and three.end - three.start == datetime.timedelta(days=3)
    two_weeks = resolve("past 2 weeks", NOW, LA)
    assert two_weeks is not None and two_weeks.end - two_weeks.start == datetime.timedelta(days=14)
    monday = resolve("monday", NOW, LA)
    assert monday is not None and monday.start == _utc(2026, 9, 28, 7)
    wednesday = resolve("wednesday", NOW, LA)  # today is Wednesday: the previous one
    assert wednesday is not None and wednesday.start == _utc(2026, 9, 23, 7)
    iso = resolve("2026-09-01", NOW, LA)
    assert iso is not None and iso.start == _utc(2026, 9, 1, 7)
    assert resolve("someday", NOW, LA) is None and resolve(None, NOW, LA) is None


def test_local_days_follow_daylight_saving_changes() -> None:
    nov1 = resolve("2026-11-01", NOW, LA)  # PDT -> PST: a 25-hour day
    assert nov1 is not None and nov1.end - nov1.start == datetime.timedelta(hours=25)
    bad_tz = resolve("today", NOW, "Not/AZone")
    assert bad_tz is not None and bad_tz.start == _utc(2026, 9, 30)  # unknown zone → UTC


def test_since_and_checkpoints() -> None:
    w = since("monday", NOW, LA)
    assert w is not None and (w.start, w.end) == (_utc(2026, 9, 28, 7), NOW)
    assert from_checkpoint(None, NOW).basis == "default"
    old = from_checkpoint(NOW - datetime.timedelta(days=20), NOW)
    assert old.basis == "default" and old.start == NOW - datetime.timedelta(days=7) and old.note
    recent = from_checkpoint(NOW - datetime.timedelta(days=2), NOW)
    assert recent.basis == "checkpoint" and recent.start == NOW - datetime.timedelta(days=2)


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("What did I miss since Monday?", ("monday", True)),
        ("What changed since the last meeting with Priya?", ("since_last_meeting", True)),
        ("I was away for three days, what happened?", ("last three days", True)),
        ("What happened in the last week?", ("last 7 days", False)),  # rolling, §7.1
        ("Anything over the past week?", ("last 7 days", False)),
        ("What did we decide last week?", ("last_week", False)),  # the previous Monday-Sunday
        ("What happened yesterday?", ("yesterday", False)),
        ("What did Sam say on Tuesday?", ("tuesday", False)),
        ("Who owes me a reply?", (None, False)),
    ],
)
def test_extract_expression(question: str, expected: tuple[str | None, bool]) -> None:
    assert extract_expression(question) == expected


# ---------------------------------------------------------------- planner rules


@pytest.mark.parametrize(
    ("question", "intent"),
    [
        ("What do I owe this week?", "promised"),
        ("What did I promise Priya?", "promised"),
        ("Who am I waiting on?", "waiting_for"),
        ("What does Jordan owe me?", "waiting_for"),
        ("Which emails need my reply?", "needs_response"),
        ("What is overdue?", "overdue"),
        ("Any deadlines coming up?", "deadlines"),
        ("What should I focus on today?", "next_action"),
        ("What happened yesterday?", "day_view"),
        ("What's the status of the vendor contract?", "topic_status"),
        ("How is the Atlas project going?", "project"),
    ],
)
def test_rule_intents(question: str, intent: str) -> None:
    plan = plan_by_rules(question)
    assert plan is not None and plan.intent == intent and plan.planner == "rules"


def test_email_context_needs_an_open_thread_and_names_are_extracted() -> None:
    assert plan_by_rules("What is this email about?") is None  # no open thread: AI-05 decides
    conv = uuid.uuid4()
    plan = plan_by_rules("What is this email about?", conversation_id=conv)
    assert plan is not None and plan.intent == "email_context" and plan.conversation_id == conv
    assert candidate_names("What did Priya Raman and Sam say about the Q3 Budget?")[:2] == [
        "Priya Raman",
        "Sam",
    ]
    person = plan_by_rules("Priya Raman?")
    assert person is not None and person.intent == "person" and person.person_names == ("Priya Raman",)


# ---------------------------------------------------------------- search helpers


def test_ts_terms_are_safe_for_to_tsquery() -> None:
    assert ts_terms("Budget's Q3 (draft) & numbers!! a") == "budget | q3 | draft | numbers"
    assert ts_terms(None) == "" and ts_terms("' ; DROP TABLE x; --") == "drop | table"
    assert ts_terms(" ".join(f"w{i}" for i in range(40))).count("|") == 11  # at most 12 terms


def test_diversify_caps_chunks_per_conversation() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    hits = [SimpleNamespace(conversation_id=c, n=i) for i, c in enumerate([a, a, a, a, b, None, b])]
    out = diversify(hits, limit=10, cap=2)  # type: ignore[arg-type]
    assert [h.n for h in out] == [0, 1, 4, 5, 6]
    assert len(diversify(hits, limit=2, cap=2)) == 2  # type: ignore[arg-type]


# ---------------------------------------------------------------- ranking and packing


def test_ranking_decay_authority_and_anchor() -> None:
    assert recency_decay(datetime.timedelta(days=14), "message") == pytest.approx(0.5)
    assert recency_decay(datetime.timedelta(days=400), "open_item") == 1.0  # open items do not decay
    assert recency_decay(datetime.timedelta(days=-1), "event") == 1.0
    assert (
        authority_class(origin="user", verification_status="suggested", user_fields=False, strength=None)
        == "user"
    )
    assert (
        authority_class(origin="ai", verification_status="confirmed", user_fields=False, strength=None)
        == "confirmed"
    )
    assert (
        authority_class(origin="ai", verification_status="suggested", user_fields=False, strength="explicit")
        == "explicit"
    )
    plain = score(base=1.0, kind="open_item", age=datetime.timedelta(0))
    assert score(base=1.0, kind="open_item", age=datetime.timedelta(0), authority="user") == pytest.approx(
        1.4 * plain
    )
    assert score(base=1.0, kind="open_item", age=datetime.timedelta(0), anchor_match=True) == pytest.approx(
        1.3
    )
    assert score(base=1.0, kind="open_item", age=datetime.timedelta(0), materiality=3) == pytest.approx(1.2)


def _item(key: str, priority: str, text: str, section: str = "state", score_: float = 1.0) -> PacketItem:
    return PacketItem(
        key=key,
        kind="work_item",
        section=section,  # type: ignore[arg-type]
        priority=priority,  # type: ignore[arg-type]
        text=text,
        line=text,
        data_class="ai_derived",
        claim_kind="inference",
        authority=2,
        score=score_,
    )


def test_pack_orders_numbers_dedupes_and_respects_budgets() -> None:
    big = "x" * 400  # 100 tokens
    candidates = [
        _item("a", "anchor", "anchor item", section="anchors"),
        _item("c1", "chunk", big),
        _item("o1", "other_state", big),
        _item("e1", "evidence", "quote", section="support"),
        _item("a", "chunk", "duplicate key at a lower priority"),
    ]
    p = pack(
        scenario="S6",
        frame="F",
        coverage_text="C",
        question="Q?",
        session_text="",
        candidates=candidates,
        budget=Budget(150, 400),
    )
    keys = [i.key for i in p.items]
    assert keys.count("a") == 1 and "a" in keys and "e1" in keys
    assert [i.cid for i in p.items] == [f"S{n}" for n in range(1, len(p.items) + 1)]
    assert p.items[0].section == "anchors"
    assert p.dropped  # the dynamic budget (150 tokens) leaves out lower-priority bulk
    assert p.tokens <= 400
    unbounded = pack(
        scenario="S8",
        frame="F",
        coverage_text="C",
        question="Q?",
        session_text="",
        candidates=candidates,
        budget=Budget(None, None),
    )
    assert {i.key for i in unbounded.items} == {"a", "c1", "o1", "e1"} and not unbounded.dropped
    timeline = pack(
        scenario="S6",
        frame="F",
        coverage_text="",
        question="Q",
        session_text="",
        candidates=[
            replace(_item("t2", "anchor_timeline", "later", section="timeline"), as_of=NOW),
            replace(
                _item("t1", "anchor_timeline", "earlier", section="timeline"),
                as_of=NOW - datetime.timedelta(days=1),
            ),
        ],
        budget=Budget(4000, 8000),
    )
    assert [i.key for i in timeline.items] == ["t1", "t2"]  # timelines read in time order


# ---------------------------------------------------------------- chunking (§9.9)


def test_email_chunking_splits_long_bodies_at_paragraphs() -> None:
    short = email_chunks("Subject", "One paragraph.")
    assert (
        len(short) == 1
        and short[0].kind == "email"
        and short[0].token_count == estimate_tokens(short[0].text)
    )
    paragraphs = "\n\n".join(f"Paragraph {i}. " + "word " * 300 for i in range(8))
    chunks = email_chunks("Long", paragraphs)
    assert len(chunks) > 1 and all(c.token_count <= MAX_TOKENS + 20 for c in chunks)
    assert [c.index for c in chunks] == list(range(len(chunks)))
    assert split_text("") == []
    subject_only = email_chunks("Empty", "")
    assert [c.text for c in subject_only] == ["Empty"]  # the subject alone is still searchable
