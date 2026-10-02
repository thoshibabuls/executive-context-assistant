"""Acceptance scorecard and decision rule (AI_EVALUATION.md §8; cost bands AI_COST_MODEL.md §9).

A candidate is accepted only when every pass condition holds (safety, quality per gated slice,
reliability, latency, cost, human review) and at least one improvement condition holds. Human
review is a pass condition: until a person has reviewed the sampled diffs the decision is
``incomplete``, never ``accept``.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

QUALITY_TOLERANCE = 0.02
MIN_GATED_SLICE = 30
LATENCY_TOLERANCE = 0.15
RELIABILITY_TOLERANCE = 0.005
RELIABILITY_TARGETS: dict[str, tuple[str, float]] = {
    # metric: (comparison, target) from AI_EVALUATION.md §5
    "schema_valid_first_attempt": (">=", 0.99),
    "repair_call_rate": ("<=", 0.01),
    "failed_permanent_rate": ("<=", 0.005),
    "provider_error_rate": ("<=", 0.005),
    "retry_overhead": ("<=", 0.05),
}

Decision = Literal["accept", "reject", "keep_baseline", "incomplete"]
HumanReview = Literal["not_performed", "passed", "failed"]


@dataclass(frozen=True)
class QualityRow:
    suite: str
    metric: str
    slice: str
    n_cases: int
    baseline: float
    candidate: float
    ci_low: float
    ci_high: float

    @property
    def diff(self) -> float:
        return self.candidate - self.baseline

    @property
    def gated(self) -> bool:
        return self.n_cases >= MIN_GATED_SLICE

    @property
    def regressed(self) -> bool:
        return self.diff < -QUALITY_TOLERANCE or self.ci_low < -QUALITY_TOLERANCE

    @property
    def significant_gain(self) -> bool:
        return self.ci_low > 0


@dataclass(frozen=True)
class SafetyRow:
    metric: str
    baseline: int
    candidate: int

    @property
    def passed(self) -> bool:
        return self.candidate == 0


@dataclass
class Scorecard:
    run: dict[str, Any]
    safety: list[SafetyRow] = field(default_factory=list)
    quality: list[QualityRow] = field(default_factory=list)
    reliability: dict[str, tuple[float, float]] = field(default_factory=dict)  # metric: (baseline, candidate)
    latency_p95_ms: tuple[float, float] = (0.0, 0.0)
    cost_usd: tuple[float, float] = (0.0, 0.0)
    human_review: HumanReview = "not_performed"
    not_run: list[str] = field(default_factory=list)


def cost_band(baseline: float, candidate: float) -> tuple[str, bool]:
    """(band, passes without extra sign-off) per AI_COST_MODEL.md §9."""
    if baseline == 0:
        return ("no_cost_change", True) if candidate == 0 else ("new_cost_needs_product_signoff", False)
    change = round((candidate - baseline) / baseline, 9)  # no float noise at the band edges
    if change < 0:
        return "decrease", True
    if change <= 0.05:
        return "within_5pct", True
    if change <= 0.20:
        return "5_to_20pct_needs_significant_gain_and_owner_signoff", False
    return "over_20pct_needs_product_signoff", False


def evaluate(sc: Scorecard) -> tuple[Decision, list[str]]:
    reasons: list[str] = []
    failed = False
    for s in sc.safety:
        if not s.passed:
            failed = True
            reasons.append(f"safety: {s.metric} = {s.candidate} (must be 0)")
    for q in sc.quality:
        if q.gated and q.regressed:
            failed = True
            reasons.append(f"quality: {q.suite}/{q.metric}[{q.slice}] regressed by {q.diff:+.3f}")
    reliability_gain = False
    for metric, (base, cand) in sc.reliability.items():
        op, target = RELIABILITY_TARGETS[metric]
        meets = cand >= target if op == ">=" else cand <= target
        worse = (base - cand) if op == ">=" else (cand - base)
        if not meets or worse > RELIABILITY_TOLERANCE:
            failed = True
            reasons.append(f"reliability: {metric} = {cand:.4f} (target {op} {target})")
        if -worse > RELIABILITY_TOLERANCE:
            reliability_gain = True
    base_p95, cand_p95 = sc.latency_p95_ms
    if base_p95 > 0 and cand_p95 > base_p95 * (1 + LATENCY_TOLERANCE):
        failed = True
        reasons.append(f"latency: p95 {cand_p95:.1f} ms vs baseline {base_p95:.1f} ms (> +15%)")
    band, cost_ok = cost_band(*sc.cost_usd)
    if not cost_ok:
        failed = True
        reasons.append(f"cost: {band}")
    if sc.human_review == "failed":
        failed = True
        reasons.append("human review found a systematic regression")
    if failed:
        return "reject", reasons

    improvements = [q for q in sc.quality if q.gated and q.significant_gain]
    base_cost, cand_cost = sc.cost_usd
    cost_gain = base_cost > 0 and (base_cost - cand_cost) / base_cost >= 0.10
    latency_gain = base_p95 > 0 and (base_p95 - cand_p95) / base_p95 >= 0.15
    improved = bool(improvements) or cost_gain or latency_gain or reliability_gain
    reasons += [f"improvement: {q.suite}/{q.metric}[{q.slice}] {q.diff:+.3f}" for q in improvements]
    if sc.human_review != "passed":
        reasons.append("human review of 20 sampled diffs not performed (pass condition, §8.1)")
        return "incomplete", reasons
    if not improved:
        reasons.append("no significant improvement: baseline kept")
        return "keep_baseline", reasons
    return "accept", reasons


def to_json(sc: Scorecard) -> dict[str, Any]:
    decision, reasons = evaluate(sc)
    base_cost, cand_cost = sc.cost_usd
    return {
        "run": sc.run,
        "decision": decision,
        "reasons": reasons,
        "safety": [{**asdict(s), "passed": s.passed} for s in sc.safety],
        "quality": [
            {
                **asdict(q),
                "diff": q.diff,
                "gated": q.gated,
                "regressed": q.regressed,
                "significant_gain": q.significant_gain,
            }
            for q in sc.quality
        ],
        "reliability": {k: {"baseline": b, "candidate": c} for k, (b, c) in sc.reliability.items()},
        "latency_p95_ms": {"baseline": sc.latency_p95_ms[0], "candidate": sc.latency_p95_ms[1]},
        "cost_usd": {
            "baseline": base_cost,
            "candidate": cand_cost,
            "band": cost_band(base_cost, cand_cost)[0],
        },
        "human_review": sc.human_review,
        "not_run": sc.not_run,
    }


def to_markdown(sc: Scorecard) -> str:
    data = to_json(sc)
    lines = [
        f"# Scorecard: {sc.run.get('suite', '')}",
        "",
        f"- Dataset: {sc.run.get('dataset')} ({sc.run.get('labels_status')} labels)",
        f"- Baseline: `{sc.run.get('baseline')}`; candidate: `{sc.run.get('candidate')}`",
        f"- Splits: {', '.join(sc.run.get('splits', []))}; cases: {sc.run.get('cases')}",
        f"- **Decision: {data['decision']}**",
        "",
        "## Reasons",
        "",
        *[f"- {r}" for r in data["reasons"]],
        "",
        "## Safety (zero tolerance)",
        "",
        "| Metric | Baseline | Candidate | Pass |",
        "|---|---|---|---|",
        *[
            f"| {s.metric} | {s.baseline} | {s.candidate} | {'yes' if s.passed else 'NO'} |"
            for s in sc.safety
        ],
        "",
        "## Quality (paired bootstrap, 95% CI of candidate - baseline)",
        "",
        "| Suite | Metric | Slice | n | Baseline | Candidate | Diff | CI | Gated | Regressed |",
        "|---|---|---|---|---|---|---|---|---|---|",
        *[
            f"| {q.suite} | {q.metric} | {q.slice} | {q.n_cases} | {q.baseline:.3f} | {q.candidate:.3f} | "
            f"{q.diff:+.3f} | [{q.ci_low:+.3f}, {q.ci_high:+.3f}] | {'yes' if q.gated else 'no'} | "
            f"{'YES' if q.regressed else 'no'} |"
            for q in sc.quality
        ],
        "",
        "## Reliability, latency, cost, human review",
        "",
        *[
            f"- {k}: baseline {v['baseline']:.4f}, candidate {v['candidate']:.4f}"
            for k, v in data["reliability"].items()
        ],
        f"- Latency p95: baseline {sc.latency_p95_ms[0]:.2f} ms, candidate {sc.latency_p95_ms[1]:.2f} ms",
        f"- Cost: baseline ${sc.cost_usd[0]:.4f}, candidate ${sc.cost_usd[1]:.4f} "
        f"({data['cost_usd']['band']})",
        f"- Human review: {sc.human_review}",
        "",
        "## Not run",
        "",
        *[f"- {n}" for n in sc.not_run],
        "",
    ]
    return "\n".join(lines)


def write_report(sc: Scorecard, directory: Path) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    json_path, md_path = directory / "scorecard.json", directory / "scorecard.md"
    json_path.write_text(json.dumps(to_json(sc), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(to_markdown(sc), encoding="utf-8")
    return json_path, md_path
