"""AI suite runner: baseline vs candidate on the email slice (AI_EVALUATION.md §4.1-§4.3, §8).

Computes E1 (classification) and the email statement metrics (E2/E3 subset available before
AI-01 exists) per slice, the zero-tolerance safety counts, paired-bootstrap confidence intervals
clustered by thread, and the scorecard. Pipelines are passed in; in slice 0.5 they are the
deterministic stubs.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from eca_evals.ai.stubs import USER, Prediction
from eca_evals.dataset import EmailCase, WorldV1
from eca_evals.scorecard import QualityRow, SafetyRow, Scorecard
from eca_evals.stats import (
    DEFAULT_RESAMPLES,
    macro_f1,
    paired_bootstrap,
    percentile,
    precision_recall_f1,
    safe_div,
)


class EmailPipeline(Protocol):
    name: str

    def predict(self, case: EmailCase) -> Prediction: ...


@dataclass(frozen=True)
class CaseRun:
    case: EmailCase
    prediction: Prediction
    latency_ms: float


def _run(pipeline: EmailPipeline, cases: Sequence[EmailCase]) -> list[CaseRun]:
    out = []
    for case in cases:
        started = time.perf_counter()
        prediction = pipeline.predict(case)
        out.append(CaseRun(case, prediction, (time.perf_counter() - started) * 1000))
    return out


def _gold_keys(case: EmailCase) -> Counter[tuple[str, str, str]]:
    return Counter((s["statement_kind"], s["owner"], s["direction"]) for s in case.statements)


def _pred_keys(p: Prediction) -> Counter[tuple[str, str, str]]:
    return Counter((s.statement_kind, s.owner, s.direction) for s in p.statements)


MetricFn = Callable[[list[CaseRun], Sequence[int]], float]


def needs_reply_precision(runs: list[CaseRun], idx: Sequence[int]) -> float:
    tp = sum(1 for i in idx if runs[i].prediction.needs_reply and runs[i].case.triage["needs_reply"])
    fp = sum(1 for i in idx if runs[i].prediction.needs_reply and not runs[i].case.triage["needs_reply"])
    return safe_div(tp, tp + fp)


def needs_reply_recall(runs: list[CaseRun], idx: Sequence[int]) -> float:
    tp = sum(1 for i in idx if runs[i].prediction.needs_reply and runs[i].case.triage["needs_reply"])
    fn = sum(1 for i in idx if not runs[i].prediction.needs_reply and runs[i].case.triage["needs_reply"])
    return safe_div(tp, tp + fn)


def category_macro_f1(runs: list[CaseRun], idx: Sequence[int]) -> float:
    return macro_f1(
        [runs[i].case.triage["category"] for i in idx], [runs[i].prediction.category for i in idx]
    )


def request_type_accuracy(runs: list[CaseRun], idx: Sequence[int]) -> float:
    return safe_div(
        sum(1 for i in idx if runs[i].prediction.request_type == runs[i].case.triage["request_type"]),
        len(idx),
    )


def prefilter_kept_rate(runs: list[CaseRun], idx: Sequence[int]) -> float:
    """1 - false-skip rate: relevant mail that the prefilter kept (higher is better)."""
    relevant = [i for i in idx if not runs[i].case.triage["prefilter_skip"]]
    return 1 - safe_div(sum(1 for i in relevant if runs[i].prediction.prefilter_skip), len(relevant))


def _statement_counts(runs: list[CaseRun], idx: Sequence[int]) -> tuple[int, int, int]:
    tp = fp = fn = 0
    for i in idx:
        gold, pred = _gold_keys(runs[i].case), _pred_keys(runs[i].prediction)
        hit = sum((gold & pred).values())
        tp += hit
        fp += sum(pred.values()) - hit
        fn += sum(gold.values()) - hit
    return tp, fp, fn


def statement_precision(runs: list[CaseRun], idx: Sequence[int]) -> float:
    return precision_recall_f1(*_statement_counts(runs, idx))[0]


def statement_recall(runs: list[CaseRun], idx: Sequence[int]) -> float:
    return precision_recall_f1(*_statement_counts(runs, idx))[1]


METRICS: dict[str, tuple[str, MetricFn]] = {
    "needs_reply_precision": ("E1", needs_reply_precision),
    "needs_reply_recall": ("E1", needs_reply_recall),
    "category_macro_f1": ("E1", category_macro_f1),
    "request_type_accuracy": ("E1", request_type_accuracy),
    "prefilter_kept_rate": ("E1", prefilter_kept_rate),
    "statement_precision": ("E2/E3", statement_precision),
    "statement_recall": ("E2/E3", statement_recall),
}


def _on_slice(fn: MetricFn, runs: list[CaseRun], idx: Sequence[int]) -> Callable[[Sequence[int]], float]:
    """The metric on a bootstrap sample, given as positions inside the slice ``idx``."""
    local = list(idx)
    return lambda sample: fn(runs, [local[j] for j in sample])


def safety_counts(runs: list[CaseRun]) -> dict[str, int]:
    hallucinated = invented_dates = my_commitment_for_other = forwarder_commitment = 0
    for r in runs:
        body = r.case.body
        for s in r.prediction.statements:
            if s.evidence_quote not in body:
                hallucinated += 1
            if s.due_text is not None and s.due_text not in s.evidence_quote:
                invented_dates += 1
            if s.direction == "my_commitment":
                gold_owner_is_user = any(
                    g["owner"] == USER
                    and g["direction"] == "my_commitment"
                    and g["evidence_quote"] == s.evidence_quote
                    for g in r.case.statements
                )
                if not gold_owner_is_user:
                    my_commitment_for_other += 1
                    if any(g.get("in_forwarded_content") for g in r.case.statements):
                        forwarder_commitment += 1
    return {
        "hallucinated_statements": hallucinated,
        "invented_dates": invented_dates,
        "my_commitment_for_someone_elses_promise": my_commitment_for_other,
        "forwarded_content_attributed_to_forwarder": forwarder_commitment,
    }


def run_ai_suite(
    world: WorldV1,
    case_splits: dict[str, str],
    *,
    baseline: EmailPipeline,
    candidate: EmailPipeline,
    splits: Sequence[str] = ("test", "challenge"),
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = 12345,
    labels_status: str = "draft",
) -> Scorecard:
    cases = [c for c in world.emails if case_splits.get(c.case_id) in splits]
    if not cases:
        raise ValueError(f"no cases in splits {list(splits)}")
    base_runs, cand_runs = _run(baseline, cases), _run(candidate, cases)
    slices: dict[str, list[int]] = {"all": list(range(len(cases)))}
    for i, c in enumerate(cases):
        slices.setdefault(f"sender_type={c.triage['sender_type']}", []).append(i)
        slices.setdefault(f"direction={c.triage['direction']}", []).append(i)
    quality: list[QualityRow] = []
    for slice_name, idx in slices.items():
        clusters = [cases[i].thread_id for i in idx]
        for metric, (suite, fn) in METRICS.items():
            result = paired_bootstrap(
                clusters,
                _on_slice(fn, base_runs, idx),
                _on_slice(fn, cand_runs, idx),
                resamples=resamples,
                seed=seed,
            )
            quality.append(
                QualityRow(
                    suite,
                    metric,
                    slice_name,
                    len(idx),
                    fn(base_runs, idx),
                    fn(cand_runs, idx),
                    result.ci_low,
                    result.ci_high,
                )
            )
    base_safety, cand_safety = safety_counts(base_runs), safety_counts(cand_runs)
    run: dict[str, Any] = {
        "suite": "ai/email_slice",
        "dataset": "world_v1",
        "labels_status": labels_status,
        "baseline": baseline.name,
        "candidate": candidate.name,
        "splits": list(splits),
        "cases": len(cases),
        "threads": len({c.thread_id for c in cases}),
        "resamples": resamples,
        "seed": seed,
        "stub_latency_p95_ms": {
            "baseline": round(percentile([r.latency_ms for r in base_runs], 95), 4),
            "candidate": round(percentile([r.latency_ms for r in cand_runs], 95), 4),
        },
    }
    return Scorecard(
        run=run,
        safety=[SafetyRow(k, base_safety[k], cand_safety[k]) for k in base_safety],
        quality=quality,
        reliability={  # deterministic stubs make no model calls: no schema failures, retries or errors
            "schema_valid_first_attempt": (1.0, 1.0),
            "repair_call_rate": (0.0, 0.0),
            "failed_permanent_rate": (0.0, 0.0),
            "provider_error_rate": (0.0, 0.0),
            "retry_overhead": (0.0, 0.0),
        },
        latency_p95_ms=(0.0, 0.0),
        cost_usd=(0.0, 0.0),
        not_run=[
            "Latency gate (AI_EVALUATION.md §6): measured on live runs only; stub timings are recorded in "
            "run.stub_latency_p95_ms",
            "E2/E3 description similarity, strength and deadline-resolution metrics: "
            "need AI-01 and the resolver "
            "(slice 1.4)",
            "DIR-120 contrastive set: slice 1.4",
            "E14 provenance completeness: no AI-derived objects before slice 1.4",
            "Live repetitions (3 runs) and run-to-run variance: stubs are deterministic",
        ],
    )
