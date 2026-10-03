"""Candidate rule 3 without item embeddings (CONTEXT_ARCHITECTURE.md §12.1): lexical closeness."""

from __future__ import annotations

import pytest

from eca.work.candidates import content_words, lexically_close


@pytest.mark.parametrize(
    ("title", "body", "close"),
    [
        ("Send the vendor risk report", "Heads up, John's vendor risk report will be late.", True),
        ("Send the pricing sheet", "The pricing sheet will be a few days late.", True),
        ("Send the vendor risk report", "The vendor contract is signed.", False),  # 1 of 3 words
        ("Send the report", "The report is late.", False),  # a single content word is not enough
        ("Book the venue for the offsite", "Is the offsite venue booked yet?", False),  # 2 of 3 below 75 %
        ("Review the Q3 budget", "Thanks for the Q3 budget numbers.", True),
    ],
)
def test_lexically_close(title: str, body: str, close: bool) -> None:
    assert lexically_close(title, content_words(body)) is close


def test_content_words_drop_short_and_filler_words() -> None:
    assert content_words("Send the SOC-2 bridge letter by Oct 5") == {"soc-2", "bridge", "letter", "oct"}
