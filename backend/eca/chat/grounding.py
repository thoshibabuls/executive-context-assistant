"""Deterministic grounding checks on every model answer (AI_PIPELINE.md §5.7 rules 1-7, §5.8).

Pure functions of (model output, packet). They never call a model:
1. Citation IDs must exist in the packet (``COVERAGE`` included); others are dropped.
2. Every number, month, weekday and proper name of a ``source`` claim must appear in the text of
   a cited item (normalized tokens); otherwise the claim becomes ``inference`` and is flagged.
3. ``absence`` claims must cite ``COVERAGE``; otherwise the citation is added and the coverage
   sentence is appended to the answer.
4. ``user`` claims must cite a user-backed item (created, confirmed or edited by the user).
5. ``recommendation`` claims are rendered as suggestions whatever their wording.
6. A factual question with no surviving ``source`` or ``user`` claim is answered by abstention.
7. Answer confidence is capped at ``medium`` when any claim was relabelled.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from eca.intelligence import Answer
from eca.retrieval import COVERAGE_CID, Packet

MONTHS = {
    "january": "jan", "february": "feb", "march": "mar", "april": "apr", "may": "may", "june": "jun",
    "july": "jul", "august": "aug", "september": "sep", "sept": "sep", "october": "oct", "november": "nov",
    "december": "dec",
}  # fmt: skip
DAYS = {
    "monday": "mon", "tuesday": "tue", "wednesday": "wed", "thursday": "thu", "friday": "fri",
    "saturday": "sat", "sunday": "sun",
}  # fmt: skip
SHORT = set(MONTHS.values()) | set(DAYS.values()) | {"tues", "thur", "thurs"}
_COMMON_WORDS = """
a an and as at be but by for from he her his i if in is it its no not of on or our she so that
the their them there these they this those to we you your yes also latest last next today
tomorrow yesterday status expected deadline due open done suggestion possible inferred waiting
note according based both some any all none nothing several still however currently meanwhile
then after before since until while what when where which who why how there's it's i'll you'll
they're we're he's she's
"""
COMMON = frozenset(_COMMON_WORDS.split())
_TOKEN = re.compile(r"[A-Za-z][A-Za-z'\-]*|\d+")


def _norm(token: str) -> str:
    low = token.lower().strip("'")
    if low.isdigit():
        return str(int(low))
    if low in MONTHS:
        return MONTHS[low]
    if low in DAYS:
        return DAYS[low]
    if low in ("tues",):
        return "tue"
    if low in ("thur", "thurs"):
        return "thu"
    if low.endswith("'s"):
        return low[:-2]
    return low


def tokens(text: str) -> set[str]:
    return {_norm(t) for t in _TOKEN.findall(text)}


def checked_tokens(claim: str) -> set[str]:
    """Numbers, months, weekdays and capitalized words that are not common words (rule 2)."""
    out: set[str] = set()
    for raw in _TOKEN.findall(claim):
        norm = _norm(raw)
        wanted = raw.isdigit() or norm in SHORT or (raw[0].isupper() and raw.lower().strip("'") not in COMMON)
        if wanted and norm and norm not in COMMON:
            out.add(norm)
    return out


@dataclass(frozen=True)
class VerifiedClaim:
    text: str
    citations: tuple[str, ...]
    kind: str  # after the checks
    model_kind: str  # as returned by the model
    flagged: bool = False
    reason: str | None = None


@dataclass
class Verified:
    claims: list[VerifiedClaim]
    confidence: str
    relabelled: int = 0
    abstain: bool = False
    needs_coverage_sentence: bool = False
    missing_info: str | None = None
    flags: list[str] = field(default_factory=list)


def verify(output: Answer, packet: Packet, *, factual: bool) -> Verified:
    cids = packet.by_cid()
    claims: list[VerifiedClaim] = []
    relabelled = 0
    needs_coverage = False
    for claim in output.claims:
        valid = tuple(c for c in claim.citations if c in cids or c == COVERAGE_CID)
        kind = claim.kind
        reason = None
        if len(valid) < len(claim.citations):
            reason = "unknown citation removed"
        if kind == "absence":
            if COVERAGE_CID not in valid:
                valid = (*valid, COVERAGE_CID)
                reason = "coverage added"
            needs_coverage = True
        elif kind == "source":
            cited = [cids[c] for c in valid if c in cids]
            if not cited:
                kind, reason = "inference", "no valid citation"
            else:
                known = set().union(*(tokens(i.text) for i in cited))
                missing = checked_tokens(claim.text) - known
                if missing:
                    kind, reason = "inference", "not found in the cited items"
        elif kind == "user":
            cited = [cids[c] for c in valid if c in cids]
            if not any(i.user_backed for i in cited):
                kind, reason = "inference", "no user-confirmed item cited"
        flagged = kind != claim.kind
        relabelled += int(flagged)
        claims.append(VerifiedClaim(claim.text, valid, kind, claim.kind, flagged, reason))
    confidence = output.confidence
    if relabelled and confidence == "high":
        confidence = "medium"
    grounded = any(c.kind in ("source", "user") for c in claims)
    abstain = not output.answerable or (
        factual and not grounded and not any(c.kind == "absence" for c in claims)
    )
    return Verified(
        claims=claims,
        confidence=confidence,
        relabelled=relabelled,
        abstain=abstain,
        needs_coverage_sentence=needs_coverage,
        missing_info=output.missing_info,
        flags=[c.reason for c in claims if c.reason],
    )
