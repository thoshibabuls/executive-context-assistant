"""Chain specification format (CONTEXT_EVALUATION.md §5) with strict validation.

A malformed chain (unknown keys, missing sources, evidence pointing at unknown sources,
checkpoints before the sources they assert, naive timestamps) is rejected with ``ChainError``.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


class ChainError(Exception):
    pass


def _aware(v: datetime.datetime) -> datetime.datetime:
    if v.tzinfo is None or v.utcoffset() is None:
        raise ValueError("timestamps must carry a UTC offset")
    return v


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    kind: Literal["email", "meeting_transcript", "user_action"]
    occurred_at: datetime.datetime
    sender: str | None = Field(default=None, alias="from")
    to: list[str] = []
    attendees: list[str] = []
    subject: str | None = None
    body: str | None = None
    transcript: str | None = None
    action: Literal["mark_done", "reject", "edit_due"] | None = None
    target: str | None = None  # expected item key the user acts on

    _aware = field_validator("occurred_at")(_aware)

    @model_validator(mode="after")
    def _content(self) -> Source:
        if self.kind == "email" and not (self.sender and self.body):
            raise ValueError(f"email source {self.id} needs from and body")
        if self.kind == "meeting_transcript" and not (self.transcript and self.attendees):
            raise ValueError(f"meeting source {self.id} needs transcript and attendees")
        if self.kind == "user_action" and not (self.action and self.target):
            raise ValueError(f"user action {self.id} needs action and target")
        return self

    @property
    def text(self) -> str:
        return self.body or self.transcript or ""


class ExpectedItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    type: Literal["commitment", "request", "task"] | None = None
    owner: str | None = None
    counterparty: str | None = None
    direction: Literal["my_commitment", "my_task", "waiting_for", "delegated", "observed"] | None = None
    due: datetime.date | None = None
    lifecycle: Literal["open", "done", "cancelled"] | None = None
    reported_status: str | None = None
    reported_by: str | None = None
    evidence: list[str] = []
    item_count_matching: int | None = None
    has_conflict: bool | None = None


class Checkpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    at: datetime.datetime
    items: list[ExpectedItem]

    _aware = field_validator("at")(_aware)


class Query(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    as_of: datetime.datetime
    scenario: str
    text: str
    required_sources: list[str] = []
    optional_sources: list[str] = []
    required_items: list[str] = []
    forbidden_items: list[str] = []
    required_facts: list[str] = []
    forbidden_facts: list[str] = []
    must_cite: list[str] = []
    must_mention_change: str | None = None
    must_mention_conflict: bool | None = None
    must_mention_coverage_if_absence_claimed: bool | None = None
    expected_tier: Literal["deterministic", "T1", "T2"] | None = None

    _aware = field_validator("as_of")(_aware)


class Chain(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    id: str
    title: str
    tags: list[str]
    star: bool = False
    user: dict[str, str]
    world: Literal["world_v1"]
    label_status: Literal["draft", "reviewed"]
    sources: list[Source] = Field(min_length=1)
    expected_state: list[Checkpoint] = Field(min_length=1)
    queries: list[Query] = []

    @model_validator(mode="after")
    def _references(self) -> Chain:
        ids = [s.id for s in self.sources]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate source id")
        known = set(ids)
        times = {s.id: s.occurred_at for s in self.sources}
        for cp in self.expected_state:
            for item in cp.items:
                unknown = set(item.evidence) - known
                if unknown:
                    raise ValueError(f"checkpoint item {item.key} cites unknown sources {sorted(unknown)}")
                late = [e for e in item.evidence if times[e] > cp.at]
                if late:
                    raise ValueError(f"checkpoint at {cp.at} cites sources not yet received: {late}")
        for q in self.queries:
            unknown = set(q.required_sources + q.optional_sources + q.must_cite) - known
            if unknown:
                raise ValueError(f"query {q.id} cites unknown sources {sorted(unknown)}")
        return self

    def ordered_sources(self) -> list[Source]:
        return sorted(self.sources, key=lambda s: (s.occurred_at, s.id))


def load_chain(path: Path) -> Chain:
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        return Chain.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise ChainError(f"{path.name}: {exc}") from exc


def load_chains(directory: Path) -> list[Chain]:
    chains = [load_chain(p) for p in sorted(directory.glob("CC-*.yaml"))]
    ids = [c.id for c in chains]
    if len(ids) != len(set(ids)):
        raise ChainError("duplicate chain id")
    return chains
