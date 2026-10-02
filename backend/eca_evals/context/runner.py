"""Context replay runner, level L2 (CONTEXT_EVALUATION.md §3-§4, §8.1).

Each chain's sources are fed in ``occurred_at`` order under a simulated clock with hourly
ticks; at every checkpoint the pipeline's item state is compared with the expected state.
L3 (retrieval) and L4 (answers) need the retrieval and chat pipelines (Phase 2) and are reported
as not run.

Pipelines in slice 0.5 are stubs: ``OracleStub`` returns the expected state (it proves the
harness end to end) and ``NaiveStub`` creates one item per commitment-like source and ignores
user actions (the metrics must catch its duplicates and missing user authority).
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from eca_evals.clock import SimulatedClock
from eca_evals.context.chains import Chain, ExpectedItem, Source
from eca_evals.scorecard import QualityRow, SafetyRow, Scorecard
from eca_evals.stats import DEFAULT_RESAMPLES, paired_bootstrap, safe_div

ASSERTED_FIELDS = (
    "type",
    "owner",
    "counterparty",
    "direction",
    "due",
    "lifecycle",
    "reported_status",
    "has_conflict",
)


@dataclass(frozen=True)
class ItemState:
    item_id: str
    owner: str | None
    type: str | None = None
    counterparty: str | None = None
    direction: str | None = None
    due: datetime.date | None = None
    lifecycle: str = "open"
    reported_status: str | None = None
    has_conflict: bool | None = None
    evidence: frozenset[str] = frozenset()
    closed_by_user: bool = False


class ContextPipeline(Protocol):
    name: str

    def start(self, chain: Chain) -> None: ...

    def ingest(self, source: Source, now: datetime.datetime) -> None: ...

    def tick(self, now: datetime.datetime) -> None: ...

    def state(self, now: datetime.datetime) -> list[ItemState]: ...


class OracleStub:
    """Returns the expected state of the latest checkpoint at or before 'now'."""

    name = "oracle_stub"

    def start(self, chain: Chain) -> None:
        self._chain = chain
        self._user_closed: set[str] = {
            s.target for s in chain.sources if s.action == "mark_done" and s.target
        }

    def ingest(self, source: Source, now: datetime.datetime) -> None:
        return None

    def tick(self, now: datetime.datetime) -> None:
        return None

    def state(self, now: datetime.datetime) -> list[ItemState]:
        merged: dict[str, dict[str, Any]] = {}
        for cp in self._chain.expected_state:
            if cp.at > now:
                break
            for item in cp.items:
                fields = {k: v for k, v in item.model_dump().items() if v is not None and v != []}
                merged.setdefault(item.key, {}).update(fields)
        out = []
        for key, f in merged.items():
            out.append(
                ItemState(
                    item_id=key,
                    owner=f.get("owner"),
                    type=f.get("type"),
                    counterparty=f.get("counterparty"),
                    direction=f.get("direction"),
                    due=f.get("due"),
                    lifecycle=f.get("lifecycle", "open"),
                    reported_status=f.get("reported_status"),
                    has_conflict=f.get("has_conflict"),
                    evidence=frozenset(f.get("evidence", [])),
                    closed_by_user=key in self._user_closed,
                )
            )
        return out


_COMMITMENT = re.compile(r"\b(I will|I'll|We'll|We will|will send|will deliver)\b", re.IGNORECASE)


class NaiveStub:
    """One new item per commitment-like source; owner = speaker; ignores user actions."""

    name = "naive_stub"

    def start(self, chain: Chain) -> None:
        self._items: list[ItemState] = []
        self._speakers = {s.id: s.sender for s in chain.sources}

    def ingest(self, source: Source, now: datetime.datetime) -> None:
        if source.kind == "user_action":
            return
        if _COMMITMENT.search(source.text):
            owner = source.sender
            if source.kind == "meeting_transcript":
                owner = next((a for a in source.attendees if a != "p_user"), None)
            self._items.append(
                ItemState(item_id=f"n{len(self._items) + 1}", owner=owner, evidence=frozenset({source.id}))
            )

    def tick(self, now: datetime.datetime) -> None:
        return None

    def state(self, now: datetime.datetime) -> list[ItemState]:
        return list(self._items)


@dataclass
class CheckpointResult:
    chain_id: str
    at: datetime.datetime
    expected_items: int
    matched_items: int
    field_mismatches: list[str] = field(default_factory=list)
    duplicates: int = 0
    false_closures: int = 0
    expected_links: int = 0
    found_links: int = 0
    actual_links: int = 0
    correct_links: int = 0

    @property
    def state_correct(self) -> bool:
        return (
            self.matched_items == self.expected_items and not self.field_mismatches and self.duplicates == 0
        )


def _eligible(owner: str | None, want_evidence: set[str], actual: ItemState) -> bool:
    if owner is not None and actual.owner != owner:
        return False
    return not want_evidence or bool(want_evidence & actual.evidence)


def _score(item: ExpectedItem, want_evidence: set[str], actual: ItemState) -> int:
    fields = sum(
        1
        for name in ASSERTED_FIELDS
        if getattr(item, name) is not None and getattr(actual, name) == getattr(item, name)
    )
    return fields + len(want_evidence & actual.evidence)


def _check(
    chain: Chain, at: datetime.datetime, expected: Sequence[ExpectedItem], actual: list[ItemState]
) -> CheckpointResult:
    """Compare the pipeline state with one checkpoint.

    Expected and actual items are matched one to one, best score first (asserted fields that
    agree plus shared evidence). An actual item is eligible for an expected item when the owner
    agrees and it shares evidence with it; eligible items left unmatched are duplicates.
    """
    res = CheckpointResult(chain.id, at, len(expected), 0)
    # The owner persists across checkpoints: take it from the earliest mention.
    first_owner: dict[str, str | None] = {}
    for cp in chain.expected_state:
        for item in cp.items:
            if item.owner is not None:
                first_owner.setdefault(item.key, item.owner)
    pairs: list[tuple[int, int, int]] = []
    eligible: dict[int, set[int]] = {}
    for i, item in enumerate(expected):
        owner = item.owner or first_owner.get(item.key)
        want_evidence = set(item.evidence)
        for j, candidate in enumerate(actual):
            if _eligible(owner, want_evidence, candidate):
                eligible.setdefault(i, set()).add(j)
                pairs.append((-_score(item, want_evidence, candidate), i, j))
    match: dict[int, int] = {}
    used: set[int] = set()
    for _, i, j in sorted(pairs):
        if i not in match and j not in used:
            match[i] = j
            used.add(j)
    duplicates: set[int] = set()
    for i, item in enumerate(expected):
        want_ev = set(item.evidence)
        res.expected_links += len(want_ev)
        if i not in match:
            res.field_mismatches.append(f"{item.key}: no item")
            continue
        res.matched_items += 1
        best = actual[match[i]]
        duplicates |= eligible[i] - used
        for name in ASSERTED_FIELDS:
            want = getattr(item, name)
            if want is not None and getattr(best, name) != want:
                res.field_mismatches.append(
                    f"{item.key}.{name}: expected {want!r}, got {getattr(best, name)!r}"
                )
        if want_ev:
            res.found_links += len(want_ev & best.evidence)
            res.actual_links += len(best.evidence)
            res.correct_links += len(want_ev & best.evidence)
            if set(best.evidence) != want_ev:
                res.field_mismatches.append(
                    f"{item.key}.evidence: expected {sorted(want_ev)}, got {sorted(best.evidence)}"
                )
    res.duplicates = len(duplicates)
    res.false_closures = sum(1 for a in actual if a.lifecycle == "done" and not a.closed_by_user)
    return res


def replay(chain: Chain, pipeline: ContextPipeline) -> list[CheckpointResult]:
    sources = chain.ordered_sources()
    start = min([s.occurred_at for s in sources] + [cp.at for cp in chain.expected_state])
    clock = SimulatedClock(start - datetime.timedelta(minutes=1))
    pipeline.start(chain)
    events: list[tuple[datetime.datetime, int, Any]] = [(s.occurred_at, 0, s) for s in sources]
    events += [(cp.at, 1, cp) for cp in chain.expected_state]
    results = []
    for moment, kind, obj in sorted(events, key=lambda e: (e[0], e[1])):
        for tick in clock.ticks_until(moment):
            pipeline.tick(tick)
        if kind == 0:
            pipeline.ingest(obj, clock.now())
        else:
            results.append(_check(chain, obj.at, obj.items, pipeline.state(clock.now())))
    return results


def _metrics(per_chain: dict[str, list[CheckpointResult]], chains: Sequence[str]) -> dict[str, float]:
    rows = [r for c in chains for r in per_chain[c]]
    return {
        "state_accuracy": safe_div(sum(r.state_correct for r in rows), len(rows)),
        "link_recall": safe_div(sum(r.found_links for r in rows), sum(r.expected_links for r in rows)),
        "link_precision": safe_div(sum(r.correct_links for r in rows), sum(r.actual_links for r in rows)),
        "duplicate_free_rate": 1
        - safe_div(sum(r.duplicates for r in rows), sum(r.expected_items for r in rows)),
    }


def _on_chains(
    results: dict[str, list[CheckpointResult]], ids: list[str], metric: str
) -> Callable[[Sequence[int]], float]:
    return lambda sample: _metrics(results, [ids[i] for i in sample])[metric]


def run_context_suite(
    chains: Sequence[Chain],
    chain_splits: dict[str, str],
    *,
    baseline: ContextPipeline,
    candidate: ContextPipeline,
    splits: Sequence[str] = ("dev", "test", "sealed"),
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = 12345,
) -> tuple[Scorecard, dict[str, list[CheckpointResult]]]:
    selected = [c for c in chains if chain_splits.get(c.id) in splits]
    if not selected:
        raise ValueError("no chains selected")
    base = {c.id: replay(c, baseline) for c in selected}
    cand = {c.id: replay(c, candidate) for c in selected}
    ids = [c.id for c in selected]
    quality: list[QualityRow] = []
    for metric in ("state_accuracy", "link_recall", "link_precision", "duplicate_free_rate"):
        result = paired_bootstrap(
            ids,
            _on_chains(base, ids, metric),
            _on_chains(cand, ids, metric),
            resamples=resamples,
            seed=seed,
        )
        quality.append(
            QualityRow(
                "L2",
                metric,
                "all chains",
                len(ids),
                _metrics(base, ids)[metric],
                _metrics(cand, ids)[metric],
                result.ci_low,
                result.ci_high,
            )
        )
    star_failures = sum(1 for c in selected if c.star for r in cand[c.id] if not r.state_correct)
    safety = [
        SafetyRow(
            "false_closure",
            sum(r.false_closures for rs in base.values() for r in rs),
            sum(r.false_closures for rs in cand.values() for r in rs),
        ),
        SafetyRow(
            "star_chain_state_failures",
            sum(1 for c in selected if c.star for r in base[c.id] if not r.state_correct),
            star_failures,
        ),
    ]
    sc = Scorecard(
        run={
            "suite": "context/L2",
            "dataset": "world_v1",
            "labels_status": "draft",
            "baseline": baseline.name,
            "candidate": candidate.name,
            "splits": list(splits),
            "cases": len(ids),
            "checkpoints": sum(len(v) for v in cand.values()),
            "resamples": resamples,
            "seed": seed,
        },
        safety=safety,
        quality=quality,
        not_run=[
            f"L3 retrieval and L4 answers for {sum(len(c.queries) for c in selected)} queries: "
            "need retrieval and chat (Phase 2)",
            "Authority-violation metric: needs user edits in chains beyond mark_done (later chains)",
            "Slices below 30 chains are reported, not gated (AI_EVALUATION.md §8.3)",
        ],
    )
    return sc, cand


def checkpoint_report(results: dict[str, list[CheckpointResult]]) -> list[dict[str, Any]]:
    return [
        {
            "chain": r.chain_id,
            "at": r.at.isoformat(),
            "state_correct": r.state_correct,
            "mismatches": r.field_mismatches,
            "duplicates": r.duplicates,
            "false_closures": r.false_closures,
        }
        for rs in results.values()
        for r in rs
    ]


__all__ = [
    "CheckpointResult",
    "ContextPipeline",
    "ItemState",
    "NaiveStub",
    "OracleStub",
    "checkpoint_report",
    "replay",
    "run_context_suite",
]
