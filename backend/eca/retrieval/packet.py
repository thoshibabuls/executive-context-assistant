"""Context packets: items, budgets, packing and the fixed layout (CONTEXT_ARCHITECTURE.md §9.3-§9.6).

Packing (§4.2, §9.10): frame, coverage and question always; anchors and their timelines up to
the hard cap; supporting evidence, session turns, other state and chunks only while the packet
stays within the dynamic budget. An item that does not fit is skipped, never cut. Deterministic
scenarios have no token budget. Citation IDs ``S1``…``Sn`` follow the layout order.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Literal
from uuid import UUID

from eca.retrieval.chunking import estimate_tokens

Section = Literal["anchors", "state", "timeline", "support"]
Priority = Literal["anchor", "anchor_timeline", "evidence", "other_state", "chunk"]
SECTIONS: tuple[Section, ...] = ("anchors", "state", "timeline", "support")
PACK_ORDER: tuple[Priority, ...] = ("anchor", "anchor_timeline", "evidence", "other_state", "chunk")
UNBOUNDED_PRIORITIES = frozenset({"anchor", "anchor_timeline"})
COVERAGE_CID = "COVERAGE"


@dataclass(frozen=True)
class Budget:
    dynamic: int | None  # None: deterministic scenario, no token budget
    hard: int | None


# CONTEXT_ARCHITECTURE.md §9.6 (scenario → dynamic budget, hard cap).
BUDGETS: dict[str, Budget] = {
    "S1": Budget(2000, 4000),
    "S2": Budget(3000, 6000),
    "S3": Budget(5000, 10000),
    "S6": Budget(6000, 14000),
    "S7": Budget(None, None),
    "S8": Budget(None, None),
    "S9": Budget(None, None),
    "S10": Budget(4000, 8000),
    "S11": Budget(5000, 10000),
    "S12": Budget(4000, 8000),
    "RG": Budget(4000, 8000),  # reply guidance, AI-08 (Phase 3, §9.6)
    "S4": Budget(2000, 4000),  # meeting preparation (Phase 4)
    "S5": Budget(7000, 16000),  # cross-meeting and meeting synthesis (Phase 4)
    "MQ": Budget(3000, 6000),  # meeting Q&A lookups (Phase 4, §9.11)
}
LIST_LIMIT = 50  # deterministic scenarios list at most this many items


@dataclass(frozen=True)
class PacketItem:
    key: str  # "<kind>:<id>", unique within a packet
    kind: str  # work_item | decision | person | conversation | meeting | project | quote | chunk | event
    section: Section
    priority: Priority
    text: str  # packet rendering; untrusted parts delimited
    line: str  # one-line display form for deterministic answers
    data_class: str  # source | ai_derived | user | computed
    claim_kind: str  # source | user | inference (deterministic answers, AI_PIPELINE.md §5.8)
    authority: int
    entity_id: UUID | None = None
    source_item_ids: tuple[UUID, ...] = ()
    evidence_ids: tuple[UUID, ...] = ()
    as_of: datetime.datetime | None = None
    score: float = 1.0
    user_backed: bool = False  # user-created, confirmed or user-set fields (§5.7 rule 4)
    group: str | None = None  # grouping label for long lists
    cid: str | None = None

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text) + 3


@dataclass(frozen=True)
class Packet:
    scenario: str
    frame: str
    coverage_text: str
    items: tuple[PacketItem, ...]  # packed, with citation IDs, in layout order
    session_text: str
    question: str
    budget: Budget
    tokens: int
    dropped: tuple[str, ...] = field(default_factory=tuple)  # keys skipped by the budget

    def by_cid(self) -> dict[str, PacketItem]:
        return {i.cid: i for i in self.items if i.cid is not None}

    def section(self, name: Section) -> list[PacketItem]:
        return [i for i in self.items if i.section == name]

    def render(self) -> str:
        parts = [f"[FRAME]\n{self.frame}", f"[COVERAGE] (cite as {COVERAGE_CID})\n{self.coverage_text}"]
        for name in SECTIONS:
            rows = self.section(name)
            body = "\n".join(f"[{i.cid}] {i.text}" for i in rows) or "(none)"
            parts.append(f"[{name.upper()}]\n{body}")
        parts.append(f"[SESSION]\n{self.session_text or '(none)'}")
        parts.append(f"[QUESTION]\n{self.question}")
        return "\n\n".join(parts)


def _order_in_section(section: Section, items: list[PacketItem]) -> list[PacketItem]:
    if section == "timeline":
        epoch = datetime.datetime.min.replace(tzinfo=datetime.UTC)
        return sorted(items, key=lambda i: (i.as_of or epoch, i.key))
    return sorted(items, key=lambda i: (-i.score, i.key))


def pack(
    *,
    scenario: str,
    frame: str,
    coverage_text: str,
    question: str,
    session_text: str,
    candidates: Sequence[PacketItem],
    budget: Budget | None = None,
) -> Packet:
    """Select, order and number the packet items for a scenario's budget."""
    budget = budget or BUDGETS.get(scenario, Budget(4000, 8000))
    unique: dict[str, PacketItem] = {}
    for item in candidates:
        current = unique.get(item.key)
        if current is None or PACK_ORDER.index(item.priority) < PACK_ORDER.index(current.priority):
            unique[item.key] = item
    total = estimate_tokens(frame) + estimate_tokens(coverage_text) + estimate_tokens(question)
    chosen: list[PacketItem] = []
    dropped: list[str] = []
    session_included = False
    for priority in PACK_ORDER:
        if priority == "other_state" and not session_included:
            # C1 session turns pack after supporting evidence (§4.2).
            session_included = True
            if budget.dynamic is not None:
                total += estimate_tokens(session_text)
        group = sorted(
            (i for i in unique.values() if i.priority == priority), key=lambda i: (-i.score, i.key)
        )
        for item in group:
            if budget.dynamic is None:
                chosen.append(item)
                continue
            limit = budget.hard if priority in UNBOUNDED_PRIORITIES else budget.dynamic
            if limit is not None and total + item.tokens <= limit:
                chosen.append(item)
                total += item.tokens
            else:
                dropped.append(item.key)
    if budget.dynamic is None:
        total += sum(i.tokens for i in chosen) + estimate_tokens(session_text)
    numbered: list[PacketItem] = []
    n = 0
    for section in SECTIONS:
        for item in _order_in_section(section, [i for i in chosen if i.section == section]):
            n += 1
            numbered.append(replace(item, cid=f"S{n}"))
    return Packet(
        scenario=scenario,
        frame=frame,
        coverage_text=coverage_text,
        items=tuple(numbered),
        session_text=session_text,
        question=question,
        budget=budget,
        tokens=total,
        dropped=tuple(dropped),
    )
