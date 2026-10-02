"""Metrics and paired-bootstrap statistics (AI_EVALUATION.md §8.3).

Paired bootstrap over clusters (threads/chains): each resample draws clusters with
replacement and recomputes both arms' metric on the same draw, giving a distribution of the
difference (candidate - baseline). 10,000 resamples and a fixed seed by default, so results are
reproducible.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Callable, Hashable, Sequence
from dataclasses import dataclass

DEFAULT_RESAMPLES = 10_000
DEFAULT_SEED = 12345


def safe_div(num: float, den: float) -> float:
    return num / den if den else 0.0


def precision_recall_f1(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    p = safe_div(tp, tp + fp)
    r = safe_div(tp, tp + fn)
    f1 = safe_div(2 * p * r, p + r)
    return p, r, f1


def macro_f1(gold: Sequence[Hashable], pred: Sequence[Hashable]) -> float:
    """Unweighted mean F1 over the classes present in ``gold``."""
    if len(gold) != len(pred):
        raise ValueError("gold and pred differ in length")
    classes = sorted(set(gold), key=str)
    if not classes:
        return 0.0
    scores = []
    for c in classes:
        tp = sum(1 for g, p in zip(gold, pred, strict=True) if g == c and p == c)
        fp = sum(1 for g, p in zip(gold, pred, strict=True) if g != c and p == c)
        fn = sum(1 for g, p in zip(gold, pred, strict=True) if g == c and p != c)
        scores.append(precision_recall_f1(tp, fp, fn)[2])
    return sum(scores) / len(scores)


def percentile(values: Sequence[float], q: float) -> float:
    """Linear-interpolation percentile (``q`` in [0, 100])."""
    if not values:
        raise ValueError("no values")
    if not 0 <= q <= 100:
        raise ValueError("q must be within [0, 100]")
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q / 100
    lo, hi = math.floor(pos), math.ceil(pos)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


@dataclass(frozen=True)
class BootstrapResult:
    diff: float
    ci_low: float
    ci_high: float
    resamples: int

    @property
    def significant(self) -> bool:
        """The 95% interval excludes zero."""
        return self.ci_low > 0 or self.ci_high < 0


Metric = Callable[[Sequence[int]], float]


def paired_bootstrap(
    clusters: Sequence[Hashable],
    baseline: Metric,
    candidate: Metric,
    *,
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = DEFAULT_SEED,
    alpha: float = 0.05,
) -> BootstrapResult:
    """Difference candidate - baseline with a percentile CI, resampling clusters.

    ``clusters[i]`` is the cluster of case ``i``; the metrics take a list of case indices (with
    repetitions) and return the metric on that multiset.
    """
    members: dict[Hashable, list[int]] = defaultdict(list)
    for i, c in enumerate(clusters):
        members[c].append(i)
    keys = sorted(members, key=str)
    if not keys:
        raise ValueError("no cases")
    everything = list(range(len(clusters)))
    observed = candidate(everything) - baseline(everything)
    rng = random.Random(seed)  # noqa: S311 - reproducible statistics, not security
    diffs = []
    for _ in range(resamples):
        sample: list[int] = []
        for _ in keys:
            sample.extend(members[keys[rng.randrange(len(keys))]])
        diffs.append(candidate(sample) - baseline(sample))
    return BootstrapResult(
        diff=observed,
        ci_low=percentile(diffs, 100 * alpha / 2),
        ci_high=percentile(diffs, 100 * (1 - alpha / 2)),
        resamples=resamples,
    )
