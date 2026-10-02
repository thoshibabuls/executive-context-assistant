"""Deterministic split assignment at thread/chain level (AI_EVALUATION.md §3.2).

``dev`` 40%, ``test`` 45%, ``sealed`` 15%, stratified by stratum; units tagged as challenge
cases go to ``challenge``. Related cases never cross splits because the unit is the thread or
chain. The order inside a stratum comes from SHA-256 of (salt, unit ID), so the assignment is
reproducible and independent of input order.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

Split = Literal["dev", "test", "sealed", "challenge"]
PROPORTIONS: tuple[tuple[Split, float], ...] = (("dev", 0.40), ("test", 0.45), ("sealed", 0.15))
SALT = "world_v1/golden-v0.1"


@dataclass(frozen=True)
class Unit:
    unit_id: str
    stratum: str
    challenge: bool = False


def _order(unit_id: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{unit_id}".encode()).hexdigest()


def _counts(n: int) -> dict[Split, int]:
    """Largest-remainder rounding of the proportions for ``n`` units."""
    raw = {name: n * share for name, share in PROPORTIONS}
    counts: dict[Split, int] = {name: int(v) for name, v in raw.items()}
    remaining = n - sum(counts.values())
    # Ties go to the earlier split in PROPORTIONS order (test before sealed).
    order = [name for name, _ in PROPORTIONS]
    by_remainder = sorted(order, key=lambda name: (-(raw[name] - counts[name]), order.index(name)))
    for name in by_remainder[:remaining]:
        counts[name] += 1
    return counts


def assign_splits(units: Iterable[Unit], *, salt: str = SALT) -> dict[str, Split]:
    by_stratum: dict[str, list[Unit]] = defaultdict(list)
    result: dict[str, Split] = {}
    seen: set[str] = set()
    for unit in units:
        if unit.unit_id in seen:
            raise ValueError(f"duplicate unit {unit.unit_id}")
        seen.add(unit.unit_id)
        if unit.challenge:
            result[unit.unit_id] = "challenge"
        else:
            by_stratum[unit.stratum].append(unit)
    for stratum in sorted(by_stratum):
        members = sorted(by_stratum[stratum], key=lambda u: _order(u.unit_id, salt))
        counts = _counts(len(members))
        i = 0
        for name, _ in PROPORTIONS:
            for unit in members[i : i + counts[name]]:
                result[unit.unit_id] = name
            i += counts[name]
    return result


def split_shares(assignment: Mapping[str, Split]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for split in assignment.values():
        counts[split] += 1
    return dict(sorted(counts.items()))
