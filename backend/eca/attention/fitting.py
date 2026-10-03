"""Bounded, deterministic weight fitting on preference pairs (TECHNICAL_DESIGN.md §12.7-§12.8).

Pure functions: no database, no randomness. The score stays ``Σ wᵢ·mᵢ·fᵢ`` with template reasons;
fitting only finds the multipliers ``mᵢ`` that make preferred items score higher, with a pull
towards 1 and a clamp after every step, so a few pairs cannot swing the formula.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

ITERATIONS = 200
LEARNING_RATE = 0.5
L2 = 0.1
USER_BOUNDS = (0.5, 2.0)  # per-user learning bounds (§12.7)
GLOBAL_BOUNDS = (0.0, 5.0)  # offline fit of config/priority.yaml (§12.8)

Features = Mapping[str, float]


@dataclass(frozen=True)
class Pair:
    preferred: Features
    other: Features


@dataclass(frozen=True)
class Fit:
    multipliers: dict[str, float]
    agreement: float  # share of pairs where the preferred side scores higher after the fit
    pairs: int


def effective_weights(weights: Mapping[str, float], multipliers: Mapping[str, float]) -> dict[str, float]:
    """``wᵢ·mᵢ`` renormalized to sum 1 (falls back to the base weights if everything is 0)."""
    raw = {k: w * multipliers.get(k, 1.0) for k, w in weights.items()}
    total = sum(raw.values())
    return {k: v / total for k, v in raw.items()} if total > 0 else dict(weights)


def score_of(weights: Mapping[str, float], features: Features) -> float:
    return sum(weights.get(k, 0.0) * v for k, v in features.items())


def agreement(weights: Mapping[str, float], pairs: Sequence[Pair]) -> float:
    if not pairs:
        return 0.0
    wins = sum(1 for p in pairs if score_of(weights, p.preferred) > score_of(weights, p.other))
    return wins / len(pairs)


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def fit_multipliers(
    weights: Mapping[str, float],
    pairs: Sequence[Pair],
    *,
    bounds: tuple[float, float] = USER_BOUNDS,
    iterations: int = ITERATIONS,
    learning_rate: float = LEARNING_RATE,
    l2: float = L2,
) -> Fit:
    """Pairwise logistic loss on ``Σ wᵢ·mᵢ·(fᵢ(preferred) - fᵢ(other))``, full-batch gradient
    descent in a fixed feature order, L2 pull towards 1, clamp to ``bounds`` after every step."""
    names = sorted(weights)
    m = {k: 1.0 for k in names}
    lo, hi = bounds
    if pairs:
        diffs = [
            {k: weights[k] * (p.preferred.get(k, 0.0) - p.other.get(k, 0.0)) for k in names} for p in pairs
        ]
        scale = 10.0  # weights sum to 1: scale the margin so the sigmoid is not flat
        for _ in range(iterations):
            grad = {k: l2 * (m[k] - 1.0) for k in names}
            for d in diffs:
                margin = scale * sum(m[k] * d[k] for k in names)
                g = -(1.0 - _sigmoid(margin)) * scale / len(diffs)
                for k in names:
                    grad[k] += g * d[k]
            for k in names:
                m[k] = min(max(m[k] - learning_rate * grad[k], lo), hi)
    fitted = {k: round(v, 4) for k, v in m.items()}
    return Fit(fitted, agreement(effective_weights(weights, fitted), pairs), len(pairs))
