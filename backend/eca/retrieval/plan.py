"""Query plans and session state: the inputs of retrieval (CONTEXT_ARCHITECTURE.md §9.1, §13).

A plan names one code-defined retriever (``intent``) and its parameters. The planner (rules
first, AI-05 only when the rules cannot decide; AI_PIPELINE.md §5.8) produces it; nothing in a
plan is SQL. Scenario codes follow §9.6 and select the packet budget; tiers follow the default
routing of AI_PIPELINE.md §5.8.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

Intent = Literal[
    "waiting_for",
    "promised",
    "who_waiting_on_me",
    "needs_response",
    "overdue",
    "deadlines",
    "person",
    "topic_status",
    "project",
    "day_view",
    "what_changed",
    "next_action",
    "email_context",
    "reply_guidance",
    "unsupported",
]
Tier = Literal["deterministic", "T1", "T2"]

INTENT_SCENARIO: dict[str, str] = {
    "waiting_for": "S7",
    "promised": "S8",
    "who_waiting_on_me": "S9",
    "needs_response": "S9",
    "overdue": "S7",
    "deadlines": "S7",
    "person": "S2",
    "topic_status": "S6",
    "project": "S3",
    "day_view": "S10",
    "what_changed": "S11",
    "next_action": "S12",
    "email_context": "S1",
    "reply_guidance": "RG",
    "unsupported": "none",
}
INTENT_TIER: dict[str, Tier] = {
    "waiting_for": "deterministic",
    "promised": "deterministic",
    "who_waiting_on_me": "deterministic",
    "needs_response": "deterministic",
    "overdue": "deterministic",
    "deadlines": "deterministic",
    "person": "T1",
    "topic_status": "T2",
    "project": "T2",
    "day_view": "deterministic",
    "what_changed": "T2",
    "next_action": "T2",
    "email_context": "deterministic",
    "reply_guidance": "T2",
    "unsupported": "deterministic",
}
LIST_INTENTS = frozenset(
    {"waiting_for", "promised", "who_waiting_on_me", "needs_response", "overdue", "deadlines", "day_view"}
)
DISCOVERY_INTENTS = frozenset({"topic_status", "project"})


@dataclass(frozen=True)
class Plan:
    intent: str
    planner: str  # rules | ai | fallback | fixed
    person_names: tuple[str, ...] = ()
    person_ids: tuple[UUID, ...] = ()  # fixed anchors (People page, reply guidance; planner "fixed")
    person_role: str = "any"  # owner | counterparty | any
    pronoun: str | None = None  # person | thing: resolve through the focus map
    topic: str | None = None
    time_expression: str | None = None
    since: bool = False  # "since <time>" rather than "on/in <time>"
    needs_clarification: bool = False
    conversation_id: UUID | None = None
    qualifier: str | None = None  # topic qualifier of a list intent ("promised ... about the budget")
    confidence: float = 1.0

    @property
    def scenario(self) -> str:
        return INTENT_SCENARIO.get(self.intent, "none")

    @property
    def tier(self) -> Tier:
        if self.intent in LIST_INTENTS and self.qualifier:
            return "T1"  # templates cannot express the qualifier (AI_PIPELINE.md §8.2)
        return INTENT_TIER.get(self.intent, "deterministic")

    def trace(self) -> dict[str, Any]:
        """Content-free plan for ``retrieval_traces.plan``: no names, topics or question text."""
        return {
            "intent": self.intent,
            "scenario": self.scenario,
            "tier": self.tier,
            "planner": self.planner,
            "person_count": len(self.person_names) + len(self.person_ids),
            "person_role": self.person_role,
            "pronoun": self.pronoun,
            "has_topic": self.topic is not None,
            "time_expression": self.time_expression,
            "since": self.since,
            "has_qualifier": self.qualifier is not None,
            "conversation_anchor": self.conversation_id is not None,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class SessionScope:
    kind: str = "global"  # global | meeting
    meeting_id: UUID | None = None


@dataclass(frozen=True)
class Turn:
    question: str
    answer: str


@dataclass(frozen=True)
class FocusEntry:
    type: str  # person | work_item | decision | project | conversation | meeting
    id: UUID
    label: str
    turn: int

    def as_json(self) -> dict[str, Any]:
        return {"type": self.type, "id": str(self.id), "label": self.label, "turn": self.turn}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> FocusEntry:
        return cls(
            str(data["type"]), UUID(str(data["id"])), str(data.get("label", "")), int(data.get("turn", 0))
        )


@dataclass(frozen=True)
class SessionState:
    scope: SessionScope = field(default_factory=SessionScope)
    turns: tuple[Turn, ...] = ()  # last 4, oldest first
    focus: tuple[FocusEntry, ...] = ()  # newest first
    last_active_at: datetime.datetime | None = None
