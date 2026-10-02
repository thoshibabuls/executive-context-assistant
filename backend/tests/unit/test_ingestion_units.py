"""Slice 1.3 unit and failure-path tests: cleaning, prefilter, identity rules, stage machine,
fake connector paging and cursors, ``.eml`` mapping, DTO validation."""

from __future__ import annotations

import datetime

import pytest

from eca.communication import PrefilterInput, clean_body, decide, html_to_text, split_forwarded
from eca.connectors import FakeConnectorFailure, FakeFeed, NormalizedMessage, NormalizedPerson, parse_eml
from eca.ingestion import InvalidStageTransition, check_transition
from eca.people import is_public_domain, normalize_alias, normalize_email
from eca.platform.errors import CursorExpired
from tests.pipeline.support import world_v1_messages

UTC = datetime.UTC


# --- cleaning ---------------------------------------------------------------------------------


def test_quoted_history_and_signature_are_removed() -> None:
    body = (
        "I'll send the numbers by Friday.\n\nThanks,\nRaj\n-- \nRaj Menon | Kestrel Bank\n\n"
        "On Mon, Sep 21, 2026 at 9:00 AM Avery wrote:\n> Could you send the numbers?\n"
    )
    assert clean_body(body) == "I'll send the numbers by Friday.\n\nThanks,\nRaj"
    assert clean_body("Sure.\n> I'll send it Friday\n> (quoted)\nBest") == "Sure.\nBest"
    assert clean_body("Ok\n\n-----Original Message-----\nFrom: x\nI'll send it") == "Ok"
    assert clean_body("On it\n\nSent from my iPhone") == "On it"


def test_forwarded_blocks_are_kept_for_attribution() -> None:
    body = (
        "FYI\n\n---------- Forwarded message ---------\nFrom: Mei Lin <mei@x.example>\n\n"
        "I will send the report.\n"
    )
    cleaned = clean_body(body)
    own, forwarded = split_forwarded(cleaned)
    assert own == "FYI"
    assert forwarded is not None and "I will send the report." in forwarded


def test_html_bodies_are_converted() -> None:
    markup = (
        "<html><head><style>p{}</style></head><body><p>Hello&nbsp;Avery</p>"
        "<div>Can you review?</div></body></html>"
    )
    assert "Can you review?" in html_to_text(markup)
    assert "p{}" not in html_to_text(markup)
    assert clean_body(None, markup).startswith("Hello")


# --- prefilter ----------------------------------------------------------------------------------


def _p(**kw: object) -> PrefilterInput:
    base: dict[str, object] = {
        "outbound": False,
        "sender_email": "raj@kestrelbank.example",
        "categories": ("inbox",),
        "headers": {},
        "subject": "Numbers",
    }
    base.update(kw)
    return PrefilterInput(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"headers": {"List-Unsubscribe": "<u>"}}, "bulk_header"),
        ({"headers": {"Precedence": "bulk"}}, "bulk_header"),
        ({"headers": {"Auto-Submitted": "auto-generated"}}, "auto_submitted"),
        ({"sender_email": "no-reply@notify.example.com"}, "no_reply_sender"),
        ({"categories": ("inbox", "promotions")}, "category:promotions"),
        ({"categories": ("inbox", "social")}, "category:social"),
        ({"subject": "Invitation: Sync @ Mon"}, "calendar_invitation"),
    ],
)
def test_prefilter_rules_skip_with_a_reason(kw: dict[str, object], reason: str) -> None:
    decision = decide(_p(**kw))
    assert decision.skip and decision.reason == reason


def test_outbound_mail_is_never_prefiltered_except_auto_replies() -> None:
    assert not decide(
        _p(outbound=True, categories=("sent", "promotions"), headers={"List-Unsubscribe": "<u>"})
    ).skip
    assert (
        decide(_p(outbound=True, headers={"Auto-Submitted": "auto-replied"})).reason == "outbound_auto_reply"
    )
    assert not decide(_p(sender_is_vip=True, categories=("promotions",))).skip
    assert not decide(_p()).skip
    assert decide(_p(headers={"Auto-Submitted": "no"})).skip is False


# --- identity rules ---------------------------------------------------------------------------


def test_email_normalization_applies_gmail_rules_only_to_gmail() -> None:
    assert normalize_email("Alex.R+news@GoogleMail.com") == "alexr@gmail.com"
    assert normalize_email(" Alex.R+news@kestrelbank.example ") == "alex.r+news@kestrelbank.example"
    with pytest.raises(ValueError):
        normalize_email("not-an-address")
    assert normalize_alias('  "Jon   Smith" ') == "jon smith"
    assert is_public_domain("gmail.com") and is_public_domain("notify.example.com")
    assert not is_public_domain("kestrelbank.example")


# --- stage machine --------------------------------------------------------------------------


def test_stage_machine_allows_only_forward_transitions() -> None:
    check_transition("fetched", "extract_pending")
    check_transition("extract_pending", "extracted")
    check_transition("extracted", "applied")
    check_transition("applied", "extracted")  # R2 re-apply
    for current, new in [("skipped", "extract_pending"), ("fetched", "applied"), ("extracted", "fetched")]:
        with pytest.raises(InvalidStageTransition):
            check_transition(current, new)


# --- fake connector -------------------------------------------------------------------------


def _m(i: int) -> NormalizedMessage:
    return NormalizedMessage(
        external_id=f"M{i}",
        thread_external_id=f"T{i}",
        rfc822_id=f"<m{i}@x.example>",
        in_reply_to=None,
        sent_at=datetime.datetime(2026, 9, 1, 9, i, tzinfo=UTC),
        sender=NormalizedPerson("a@x.example"),
        to=(),
        cc=(),
        subject="s",
        body_text="b",
        body_html=None,
        categories=("inbox",),
    )


def test_fake_feed_pages_cursors_and_failures() -> None:
    feed: FakeFeed[NormalizedMessage] = FakeFeed(page_size=2)
    for i in range(5):
        feed.add(_m(i), delivered_at=datetime.datetime(2026, 9, 1, 10, i, tzinfo=UTC))
    now = datetime.datetime(2026, 9, 1, 10, 3, tzinfo=UTC)  # only 4 delivered yet
    first = feed.page(cursor=None, page_token=None, now=now)
    assert [m.external_id for m in first.items] == ["M0", "M1"] and first.next_page_token == "2"
    second = feed.page(cursor=None, page_token=first.next_page_token, now=now)
    assert second.next_page_token is None and second.high_water_cursor == "4"
    later = feed.page(cursor="4", page_token=None, now=datetime.datetime(2026, 9, 2, tzinfo=UTC))
    assert [m.external_id for m in later.items] == ["M4"]
    feed.fail_after_pages = feed.pages_served
    with pytest.raises(FakeConnectorFailure):
        feed.page(cursor="4", page_token=None, now=now)
    feed.expire_cursors_below = 10
    with pytest.raises(CursorExpired):
        feed.page(cursor="4", page_token=None, now=now)


def test_normalized_dto_validation_and_content_hash() -> None:
    m = _m(1)
    assert m.content_hash == _m(1).content_hash and len(m.content_hash) == 32
    with pytest.raises(ValueError, match="neutral"):
        NormalizedMessage(**{**m.__dict__, "categories": ("CATEGORY_PROMOTIONS",)})
    with pytest.raises(ValueError, match="timezone"):
        NormalizedMessage(**{**m.__dict__, "sent_at": datetime.datetime(2026, 9, 1)})


def test_world_v1_eml_mapping() -> None:
    messages = world_v1_messages()
    assert len(messages) == 150
    by_id = {m.external_id: m for m in messages}
    reply = by_id["W1-E056"]
    assert reply.categories == ("sent",) and reply.in_reply_to == "<w1-e055@world-v1.example>"
    assert reply.thread_external_id == "<w1-e055@world-v1.example>"
    bulk = by_id["W1-E102"]
    assert "promotions" in bulk.categories and bulk.headers_subset["Precedence"] == "bulk"
    assert all(m.sent_at.tzinfo is not None for m in messages)
    raw = b"From: a@x.example\nTo: b@x.example\nSubject: s\nDate: Mon, 21 Sep 2026 09:00:00 +0000\n\nbody\n"
    parsed = parse_eml(raw, external_id="E1", account_email="b@x.example")
    assert parsed.thread_external_id == "E1" and parsed.categories == ("inbox",)
    with pytest.raises(ValueError, match="no From"):
        parse_eml(b"To: b@x.example\n\nx", external_id="E2", account_email="b@x.example")
