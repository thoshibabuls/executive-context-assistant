"""Rules prefilter on neutral categories and headers (AI_PIPELINE.md §4.2 O1).

Outbound mail written by the user is never prefiltered (needed for "What did I promise?"),
except automatic replies. VIP senders bypass the prefilter. A skipped message gets a reason and
never reaches AI-01.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

_NO_REPLY = re.compile(
    r"^(no[-_.]?reply|do[-_.]?not[-_.]?reply|notifications?|mailer-daemon|postmaster)@", re.I
)
SKIP_CATEGORIES = ("promotions", "social", "updates", "spam", "trash")


@dataclass(frozen=True)
class PrefilterInput:
    outbound: bool
    sender_email: str
    categories: Sequence[str]
    headers: Mapping[str, str]
    subject: str | None
    sender_is_vip: bool = False
    self_copy: bool = False


@dataclass(frozen=True)
class PrefilterDecision:
    skip: bool
    reason: str | None
    is_bulk: bool


def _auto_submitted(headers: Mapping[str, str]) -> bool:
    value = headers.get("Auto-Submitted")
    return value is not None and value.strip().lower() != "no"


def decide(msg: PrefilterInput) -> PrefilterDecision:
    headers = msg.headers
    bulk = (
        "List-Unsubscribe" in headers
        or "List-Id" in headers
        or headers.get("Precedence", "").strip().lower() in {"bulk", "list", "junk"}
        or _auto_submitted(headers)
    )
    if msg.outbound:
        if _auto_submitted(headers) or "X-Autoreply" in headers:
            return PrefilterDecision(True, "outbound_auto_reply", False)
        return PrefilterDecision(False, None, False)
    if msg.sender_is_vip:
        return PrefilterDecision(False, None, bulk)
    if msg.self_copy:
        return PrefilterDecision(True, "self_copy", bulk)
    if (
        "List-Unsubscribe" in headers
        or "List-Id" in headers
        or headers.get("Precedence", "").lower() in {"bulk", "list", "junk"}
    ):
        return PrefilterDecision(True, "bulk_header", True)
    if _auto_submitted(headers):
        return PrefilterDecision(True, "auto_submitted", True)
    if _NO_REPLY.match(msg.sender_email.strip()):
        return PrefilterDecision(True, "no_reply_sender", True)
    for category in SKIP_CATEGORIES:
        if category in msg.categories:
            return PrefilterDecision(True, f"category:{category}", bulk)
    if msg.subject and msg.subject.lower().startswith(
        ("invitation:", "updated invitation:", "accepted:", "declined:")
    ):
        return PrefilterDecision(True, "calendar_invitation", False)
    return PrefilterDecision(False, None, bulk)
