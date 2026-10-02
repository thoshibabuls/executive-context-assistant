"""Slice 0.5: statistics, clock, splits, contamination, scorecard and manifest tools."""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest

from eca_evals.clock import SimulatedClock
from eca_evals.contamination import ngrams, scan
from eca_evals.manifest import ManifestError, build_manifest, freeze, verify_manifest, write_manifest
from eca_evals.scorecard import QualityRow, SafetyRow, Scorecard, cost_band, evaluate, to_json, to_markdown
from eca_evals.splits import Unit, assign_splits, split_shares
from eca_evals.stats import macro_f1, paired_bootstrap, percentile, precision_recall_f1, safe_div

UTC = datetime.UTC

# --- statistics ---------------------------------------------------------------------------------


def test_precision_recall_f1_known_values() -> None:
    assert precision_recall_f1(8, 2, 2) == pytest.approx((0.8, 0.8, 0.8))
    assert precision_recall_f1(3, 1, 0) == pytest.approx((0.75, 1.0, 6 / 7))
    assert precision_recall_f1(0, 0, 0) == (0.0, 0.0, 0.0)
    assert safe_div(1, 0) == 0.0


def test_macro_f1_averages_classes_present_in_gold() -> None:
    gold = ["a", "a", "b", "b"]
    pred = ["a", "b", "b", "b"]
    # class a: P=1, R=0.5, F1=2/3; class b: P=2/3, R=1, F1=0.8
    assert macro_f1(gold, pred) == pytest.approx((2 / 3 + 0.8) / 2)
    with pytest.raises(ValueError):
        macro_f1(["a"], [])


def test_percentile_linear_interpolation() -> None:
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile([5], 95) == 5
    assert percentile([0, 10], 95) == pytest.approx(9.5)
    with pytest.raises(ValueError):
        percentile([], 50)
    with pytest.raises(ValueError):
        percentile([1], 101)


def _correct_rate(flags: list[int]):  # type: ignore[no-untyped-def]
    return lambda idx: safe_div(sum(flags[i] for i in idx), len(idx))


def test_paired_bootstrap_identical_arms_has_zero_interval() -> None:
    flags = [1, 0, 1, 1, 0, 1]
    result = paired_bootstrap(list(range(6)), _correct_rate(flags), _correct_rate(flags), resamples=500)
    assert (result.diff, result.ci_low, result.ci_high) == (0.0, 0.0, 0.0)
    assert not result.significant


def test_paired_bootstrap_detects_a_clear_gain_and_is_reproducible() -> None:
    base = [0] * 20 + [1] * 20
    cand = [1] * 40
    clusters = [i // 2 for i in range(40)]  # pairs of related cases
    first = paired_bootstrap(clusters, _correct_rate(base), _correct_rate(cand), resamples=1000, seed=7)
    second = paired_bootstrap(clusters, _correct_rate(base), _correct_rate(cand), resamples=1000, seed=7)
    assert first == second
    assert first.diff == pytest.approx(0.5)
    assert first.significant and first.ci_low > 0.3


def test_paired_bootstrap_resamples_whole_clusters() -> None:
    seen: list[list[int]] = []

    def metric(idx: list[int]) -> float:
        seen.append(list(idx))
        return 0.0

    paired_bootstrap(["t1", "t1", "t2"], metric, metric, resamples=50)  # type: ignore[arg-type]
    for sample in seen[2:]:
        assert sample.count(0) == sample.count(1), "cases of one thread must be drawn together"
    with pytest.raises(ValueError):
        paired_bootstrap([], metric, metric)  # type: ignore[arg-type]


# --- simulated clock ----------------------------------------------------------------------------


def test_simulated_clock_ticks_hourly_and_never_goes_backwards() -> None:
    clock = SimulatedClock(datetime.datetime(2026, 10, 1, 9, 30, tzinfo=UTC))
    ticks = list(clock.ticks_until(datetime.datetime(2026, 10, 1, 12, 0, tzinfo=UTC)))
    assert [t.hour for t in ticks] == [10, 11]
    assert clock.now() == datetime.datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    with pytest.raises(ValueError):
        clock.advance_to(datetime.datetime(2026, 10, 1, 11, 0, tzinfo=UTC))
    with pytest.raises(ValueError):
        clock.advance(datetime.timedelta(seconds=-1))
    with pytest.raises(ValueError):
        SimulatedClock(datetime.datetime(2026, 10, 1))  # naive


# --- splits -------------------------------------------------------------------------------------


def _units() -> list[Unit]:
    return [Unit(f"t{i:03d}", "a" if i % 2 else "b") for i in range(100)] + [Unit("tc1", "a", challenge=True)]


def test_splits_are_deterministic_stratified_and_order_independent() -> None:
    units = _units()
    first = assign_splits(units)
    assert first == assign_splits(list(reversed(units)))
    assert split_shares(first) == {"challenge": 1, "dev": 40, "sealed": 14, "test": 46}
    for stratum in ("a", "b"):
        members = [first[u.unit_id] for u in units if u.stratum == stratum and not u.challenge]
        assert members.count("dev") == 20 and members.count("test") in (22, 23)
    assert first["tc1"] == "challenge"
    assert assign_splits(units, salt="other") != first


def test_duplicate_split_unit_is_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        assign_splits([Unit("t1", "a"), Unit("t1", "b")])


# --- contamination ------------------------------------------------------------------------------


def test_contamination_flags_a_copied_eight_word_span(tmp_path: Path) -> None:
    protected = {"case-1": "Could you review the Kestrel renewal contract draft before the Thursday call?"}
    clean = tmp_path / "clean.md"
    clean.write_text("Could you review the document?", encoding="utf-8")
    copied = tmp_path / "copied.md"
    copied.write_text(
        "Example: could you REVIEW the Kestrel renewal contract draft, before...", encoding="utf-8"
    )
    hits = scan(protected, [clean, copied], root=tmp_path)
    assert {h.file for h in hits} == {"copied.md"}
    assert hits[0].case_id == "case-1"
    assert len(next(iter(ngrams("one two three four five six seven eight")))) == 8


def test_contamination_allows_text_shared_with_dev(tmp_path: Path) -> None:
    boilerplate = "Thanks for the update and let me know if anything changes on your side"
    prompt = tmp_path / "prompt.md"
    prompt.write_text(boilerplate, encoding="utf-8")
    assert scan({"test-1": boilerplate}, [prompt])
    assert scan({"test-1": boilerplate}, [prompt], allowed=[boilerplate]) == []


# --- scorecard ----------------------------------------------------------------------------------


def _card(**kw: object) -> Scorecard:
    base: dict[str, object] = {
        "run": {"suite": "t", "splits": ["test"]},
        "safety": [SafetyRow("hallucinated_statements", 0, 0)],
        "quality": [QualityRow("E1", "f1", "all", 40, 0.80, 0.86, 0.02, 0.10)],
        "reliability": {"schema_valid_first_attempt": (1.0, 1.0)},
        "human_review": "passed",
    }
    base.update(kw)
    return Scorecard(**base)  # type: ignore[arg-type]


def test_scorecard_accepts_only_with_review_and_improvement() -> None:
    assert evaluate(_card())[0] == "accept"
    decision, reasons = evaluate(_card(human_review="not_performed"))
    assert decision == "incomplete" and any("human review" in r for r in reasons)
    flat = QualityRow("E1", "f1", "all", 40, 0.80, 0.80, -0.01, 0.01)
    assert evaluate(_card(quality=[flat]))[0] == "keep_baseline"


def test_scorecard_rejects_safety_regression_latency_and_cost() -> None:
    assert evaluate(_card(safety=[SafetyRow("invented_dates", 0, 1)]))[0] == "reject"
    regressed = QualityRow("E1", "f1", "all", 40, 0.80, 0.77, -0.06, 0.00)
    assert evaluate(_card(quality=[regressed]))[0] == "reject"
    assert evaluate(_card(latency_p95_ms=(1000.0, 1200.0)))[0] == "reject"
    assert evaluate(_card(cost_usd=(1.0, 1.3)))[0] == "reject"
    assert evaluate(_card(reliability={"schema_valid_first_attempt": (1.0, 0.98)}))[0] == "reject"
    assert evaluate(_card(human_review="failed"))[0] == "reject"


def test_small_slices_are_reported_but_not_gated() -> None:
    small = QualityRow("E1", "f1", "sender_type=vip", 12, 0.9, 0.5, -0.6, -0.2)
    assert small.regressed and not small.gated
    decision, _ = evaluate(_card(quality=[small, QualityRow("E1", "f1", "all", 40, 0.80, 0.86, 0.02, 0.1)]))
    assert decision == "accept"


def test_cost_bands() -> None:
    assert cost_band(0, 0) == ("no_cost_change", True)
    assert cost_band(0, 1)[1] is False
    assert cost_band(1, 0.9) == ("decrease", True)
    assert cost_band(1, 1.05) == ("within_5pct", True)
    assert cost_band(1, 1.1)[1] is False
    assert cost_band(1, 1.5)[0] == "over_20pct_needs_product_signoff"


def test_scorecard_serialises_to_json_and_markdown() -> None:
    data = to_json(_card(human_review="not_performed"))
    assert data["decision"] == "incomplete"
    json.dumps(data)
    assert "**Decision: incomplete**" in to_markdown(_card(human_review="not_performed"))


# --- manifest and freeze ------------------------------------------------------------------------


def _tree(root: Path) -> dict[str, object]:
    (root / "data" / "labels").mkdir(parents=True)
    (root / "data" / "labels" / "triage.jsonl").write_text('{"case_id": "c1"}\n', encoding="utf-8")
    (root / "data" / "c1.eml").write_text("Subject: hi\n\nhello\n", encoding="utf-8")
    manifest = build_manifest(
        root,
        version="golden-v0.1",
        covered_dirs=["data"],
        cases={"c1": "test"},
        chains={},
        label_files=["data/labels/triage.jsonl"],
    )
    write_manifest(root / "data" / "MANIFEST.json", manifest)
    return manifest


def test_manifest_verifies_and_reports_every_problem(tmp_path: Path) -> None:
    manifest = _tree(tmp_path)
    assert manifest["frozen"] is False and manifest["labels_status"] == "draft"
    assert verify_manifest(tmp_path, manifest) == []
    (tmp_path / "data" / "c1.eml").write_text("Subject: hi\n\nhello, edited\n", encoding="utf-8")
    (tmp_path / "data" / "extra.eml").write_text("x", encoding="utf-8")
    (tmp_path / "data" / "labels" / "triage.jsonl").unlink()
    kinds = {(p.kind, p.path) for p in verify_manifest(tmp_path, manifest)}
    assert kinds == {
        ("hash_mismatch", "data/c1.eml"),
        ("unlisted", "data/extra.eml"),
        ("missing", "data/labels/triage.jsonl"),
    }
    assert [
        p.kind for p in verify_manifest(tmp_path, {**manifest, "cases": {"c1": "train"}}) if p.kind == "split"
    ]
    frozen_draft = {**manifest, "frozen": True}
    assert any(p.kind == "review" for p in verify_manifest(tmp_path, frozen_draft))


def test_freeze_refuses_draft_labels_without_a_complete_review(tmp_path: Path) -> None:
    _tree(tmp_path)
    path = tmp_path / "data" / "MANIFEST.json"
    with pytest.raises(ManifestError, match="draft"):
        freeze(tmp_path, path, version="golden-v0.1")
    review = tmp_path / "data" / "REVIEW.json"
    review.write_text(
        json.dumps({"reviewers": [], "reviewed_files": [], "date": "2026-10-02"}), encoding="utf-8"
    )
    with pytest.raises(ManifestError, match="no reviewer"):
        freeze(tmp_path, path, version="golden-v0.1")
    review.write_text(
        json.dumps({"reviewers": ["Owner"], "reviewed_files": [], "date": "2026-10-02"}), encoding="utf-8"
    )
    with pytest.raises(ManifestError, match="not reviewed"):
        freeze(tmp_path, path, version="golden-v0.1")
    assert json.loads(path.read_text(encoding="utf-8"))["frozen"] is False


def test_freeze_with_complete_review_releases_the_version(tmp_path: Path) -> None:
    _tree(tmp_path)
    path = tmp_path / "data" / "MANIFEST.json"
    (tmp_path / "data" / "REVIEW.json").write_text(
        json.dumps(
            {"reviewers": ["Owner"], "reviewed_files": ["data/labels/triage.jsonl"], "date": "2026-10-02"}
        ),
        encoding="utf-8",
    )
    manifest = freeze(tmp_path, path, version="golden-v0.1")
    assert manifest["frozen"] is True and manifest["labels_status"] == "reviewed"
    assert manifest["reviewers"] == ["Owner"]
    assert verify_manifest(tmp_path, manifest) == []


def test_freeze_refuses_a_manifest_that_does_not_verify(tmp_path: Path) -> None:
    _tree(tmp_path)
    (tmp_path / "data" / "c1.eml").write_text("tampered", encoding="utf-8")
    with pytest.raises(ManifestError, match="does not verify"):
        freeze(tmp_path, tmp_path / "data" / "MANIFEST.json", version="golden-v0.1")
