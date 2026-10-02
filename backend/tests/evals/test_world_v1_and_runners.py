"""Slice 0.5: committed world_v1 data, gate A0 and the runners end to end on stub pipelines."""

from __future__ import annotations

import json
import re
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from eca_evals.__main__ import main
from eca_evals.ai.runner import run_ai_suite
from eca_evals.ai.stubs import RulesStubV1, RulesStubV2
from eca_evals.contamination import ngrams
from eca_evals.context.chains import ChainError, load_chain, load_chains
from eca_evals.context.runner import NaiveStub, OracleStub, replay, run_context_suite
from eca_evals.dataset import DatasetError, WorldV1, load_world_v1
from eca_evals.gate_a0 import run_gate_a0
from eca_evals.golden import CHAINS_REL, WORLD_REL, manifest_path
from eca_evals.manifest import load_manifest, sha256_file
from eca_evals.paths import EVALS_DIR, FEWSHOT_DIRS
from eca_evals.scorecard import evaluate
from eca_evals.world_v1 import build_world_v1

RESERVED = re.compile(r"@([a-z0-9-]+\.)*(example|test|example\.com|example\.org|example\.net)$")


@pytest.fixture(scope="module")
def world() -> WorldV1:
    return load_world_v1(EVALS_DIR / WORLD_REL)


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    return load_manifest(manifest_path(EVALS_DIR))


@pytest.fixture
def evals_copy(tmp_path: Path) -> Path:
    for rel in (WORLD_REL, CHAINS_REL):
        shutil.copytree(EVALS_DIR / rel, tmp_path / rel)
    return tmp_path


# --- committed data -----------------------------------------------------------------------------


def test_committed_data_passes_gate_a0() -> None:
    result = run_gate_a0(EVALS_DIR, scan_dirs=FEWSHOT_DIRS)
    assert result.passed, result.summary()
    statuses = {c.name: c.status for c in result.checks}
    assert statuses == {
        "manifest": "pass",
        "dataset": "pass",
        "splits": "pass",
        "contamination": "pass",
        "cassette_suites": "n/a",
        "e14_provenance": "n/a",
    }


def test_candidate_manifest_is_draft_and_not_frozen(manifest: dict[str, Any]) -> None:
    assert manifest["version"] == "golden-v0.1"
    assert manifest["frozen"] is False
    assert manifest["labels_status"] == "draft"
    assert manifest["reviewers"] == [] and manifest["review_record"] is None


def test_generator_rebuild_matches_committed_data(tmp_path: Path, manifest: dict[str, Any]) -> None:
    build_world_v1(tmp_path)
    files = manifest["files"]
    assert isinstance(files, dict)
    committed = {
        rel.removeprefix(f"{WORLD_REL}/"): h for rel, h in files.items() if rel.startswith(f"{WORLD_REL}/")
    }
    rebuilt = {p.relative_to(tmp_path).as_posix(): sha256_file(p) for p in tmp_path.rglob("*") if p.is_file()}
    assert rebuilt == committed


def test_dataset_size_and_labels(world: WorldV1) -> None:
    assert len(world.emails) == 150
    assert len(world.threads) == 136
    assert sum(len(c.statements) for c in world.emails) == 85
    assert all(c.triage["label_status"] == "draft" for c in world.emails)
    assert all(s["label_status"] == "draft" for c in world.emails for s in c.statements)
    categories = Counter(c.triage["category"] for c in world.emails)
    assert len(categories) >= 5


def test_dataset_is_synthetic_with_reserved_domains(world: WorldV1) -> None:
    addresses = [p["email"] for p in world.scenario["people"]]
    addresses += [a for c in world.emails for a in (c.sender, *c.to)]
    assert addresses and all(RESERVED.search(a) for a in addresses), [
        a for a in addresses if not RESERVED.search(a)
    ]
    assert world.scenario["synthetic"] is True
    assert all("X-ECA-Synthetic" in c.headers for c in world.emails)


def test_splits_are_thread_level_and_challenge_is_separate(world: WorldV1, manifest: dict[str, Any]) -> None:
    cases = manifest["cases"]
    assert isinstance(cases, dict)
    for thread in world.threads.values():
        splits = {cases[c] for c in thread["case_ids"]}
        assert len(splits) == 1
        assert (splits == {"challenge"}) == bool(thread["challenge_tags"])
    shares = Counter(cases.values())
    assert set(shares) == {"dev", "test", "sealed", "challenge"}
    assert manifest["chains"] and set(manifest["chains"].values()) <= {"dev", "test", "sealed"}


def test_all_ten_chains_load() -> None:
    chains = load_chains(EVALS_DIR / CHAINS_REL)
    assert [c.id for c in chains] == [f"CC-{i:02d}" for i in range(1, 11)]
    assert all(c.label_status == "draft" for c in chains)
    assert sum(c.star for c in chains) >= 1


# --- gate A0 failure paths ----------------------------------------------------------------------


def test_hash_mismatch_fails_gate_a0(evals_copy: Path) -> None:
    eml = sorted((evals_copy / WORLD_REL / "sources" / "emails").glob("*.eml"))[0]
    eml.write_bytes(eml.read_bytes().replace(b"\n\n", b"\n\nEdited. ", 1))
    result = run_gate_a0(evals_copy, scan_dirs=[])
    checks = {c.name: c for c in result.checks}
    assert not result.passed and checks["manifest"].status == "fail"
    assert any("hash_mismatch" in d for d in checks["manifest"].details)


def test_unlisted_file_fails_gate_a0(evals_copy: Path) -> None:
    (evals_copy / CHAINS_REL / "notes.txt").write_text("scratch", encoding="utf-8")
    result = run_gate_a0(evals_copy, scan_dirs=[])
    assert not result.passed
    assert any("unlisted" in d for c in result.checks for d in c.details)


def test_missing_manifest_fails_gate_a0(evals_copy: Path) -> None:
    manifest_path(evals_copy).unlink()
    result = run_gate_a0(evals_copy, scan_dirs=[])
    assert not result.passed and result.checks[0].name == "manifest"


def test_missing_labels_fail_loading_and_gate_a0(evals_copy: Path) -> None:
    triage = evals_copy / WORLD_REL / "labels" / "triage.jsonl"
    lines = triage.read_text(encoding="utf-8").splitlines(keepends=True)
    triage.write_text("".join(lines[1:]), encoding="utf-8")
    with pytest.raises(DatasetError, match="no triage label"):
        load_world_v1(evals_copy / WORLD_REL)
    result = run_gate_a0(evals_copy, scan_dirs=[])
    assert {c.name: c.status for c in result.checks}["dataset"] == "fail"
    (evals_copy / WORLD_REL / "labels" / "items.jsonl").unlink()
    with pytest.raises(DatasetError, match="missing label file"):
        load_world_v1(evals_copy / WORLD_REL)


def test_label_with_quote_not_in_email_is_rejected(evals_copy: Path) -> None:
    items = evals_copy / WORLD_REL / "labels" / "items.jsonl"
    rows = [json.loads(line) for line in items.read_text(encoding="utf-8").splitlines()]
    rows[0]["evidence_quote"] = "A sentence that the email never contains."
    items.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    with pytest.raises(DatasetError, match="evidence quote not found"):
        load_world_v1(evals_copy / WORLD_REL)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda t: t.replace("id: CC-01\n", "id: CC-01\nunexpected_key: 1\n"), "unexpected_key"),
        (lambda t: t.replace("evidence: [m1]}", "evidence: [m9]}"), "unknown sources"),
        (
            lambda t: t.replace("occurred_at: 2026-09-28T10:00:00-07:00", "occurred_at: 2026-09-28T10:00:00"),
            "UTC offset",
        ),
        (lambda t: t.replace("evidence: [m1, e1, e2]", "evidence: [m1, e1, e2]]"), "CC-01"),
        (lambda t: t.replace("kind: meeting_transcript", "kind: fax"), "kind"),
    ],
)
def test_malformed_chain_yaml_is_rejected(evals_copy: Path, mutation, message: str) -> None:  # type: ignore[no-untyped-def]
    path = next((evals_copy / CHAINS_REL).glob("CC-01_*.yaml"))
    path.write_text(mutation(path.read_text(encoding="utf-8")), encoding="utf-8")
    with pytest.raises(ChainError, match=message):
        load_chain(path)
    result = run_gate_a0(evals_copy, scan_dirs=[])
    assert {c.name: c.status for c in result.checks}["dataset"] == "fail"


def test_contamination_from_a_test_case_fails_gate_a0(evals_copy: Path, world: WorldV1) -> None:
    manifest = load_manifest(manifest_path(evals_copy))
    dev_grams: set[tuple[str, ...]] = set()
    for c in world.emails:
        if manifest["cases"][c.case_id] == "dev":
            dev_grams |= ngrams(f"{c.subject}\n{c.body}")
    # A test case with case-specific text (template boilerplate shared with dev is allowed).
    test_case = max(
        (c for c in world.emails if manifest["cases"][c.case_id] == "test"),
        key=lambda c: len(ngrams(c.body) - dev_grams),
    )
    assert ngrams(test_case.body) - dev_grams
    dev_case = next(c for c in world.emails if manifest["cases"][c.case_id] == "dev")
    prompts = evals_copy / "prompts"
    prompts.mkdir()
    (prompts / "fewshot.md").write_text(f"Example email:\n{dev_case.body}\n", encoding="utf-8")
    assert run_gate_a0(evals_copy, scan_dirs=[prompts]).passed
    (prompts / "fewshot.md").write_text(f"Example email:\n{test_case.body}\n", encoding="utf-8")
    result = run_gate_a0(evals_copy, scan_dirs=[prompts])
    checks = {c.name: c for c in result.checks}
    assert checks["contamination"].status == "fail"
    assert any(test_case.case_id in d for d in checks["contamination"].details)


def test_sealed_chain_text_in_a_prompt_fails_gate_a0(evals_copy: Path) -> None:
    manifest = load_manifest(manifest_path(evals_copy))
    sealed = next(c for c in load_chains(evals_copy / CHAINS_REL) if manifest["chains"][c.id] == "sealed")
    prompts = evals_copy / "prompts"
    prompts.mkdir()
    longest = max((s.text for s in sealed.sources), key=len)
    (prompts / "system.md").write_text(longest, encoding="utf-8")
    result = run_gate_a0(evals_copy, scan_dirs=[prompts])
    assert {c.name: c.status for c in result.checks}["contamination"] == "fail"


def test_tampered_split_assignment_fails_gate_a0(evals_copy: Path) -> None:
    path = manifest_path(evals_copy)
    manifest = load_manifest(path)
    case_id = next(c for c, s in manifest["cases"].items() if s == "test")
    manifest["cases"][case_id] = "dev"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    result = run_gate_a0(evals_copy, scan_dirs=[])
    checks = {c.name: c for c in result.checks}
    assert checks["splits"].status == "fail"
    assert any(case_id in d for d in checks["splits"].details)


# --- runners end to end -------------------------------------------------------------------------


def test_ai_runner_end_to_end_on_stubs(world: WorldV1, manifest: dict[str, Any]) -> None:
    people = world.people_by_email()
    cases = manifest["cases"]
    assert isinstance(cases, dict)

    def run() -> dict[str, Any]:
        from eca_evals.scorecard import to_json

        sc = run_ai_suite(
            world, cases, baseline=RulesStubV1(people), candidate=RulesStubV2(people), resamples=300
        )
        data = to_json(sc)
        data["run"].pop("stub_latency_p95_ms")
        return data

    first, second = run(), run()
    assert first == second, "stub runs are deterministic"
    safety = {row["metric"]: row for row in first["safety"]}
    # The baseline's known flaw is caught; the candidate fixes it.
    assert safety["forwarded_content_attributed_to_forwarder"]["baseline"] > 0
    assert all(row["candidate"] == 0 for row in safety.values())
    assert first["decision"] == "incomplete"  # human review not performed
    assert first["run"]["cases"] == sum(1 for s in cases.values() if s in ("test", "challenge"))


def test_ai_runner_rejects_a_candidate_with_a_safety_failure(
    world: WorldV1, manifest: dict[str, Any]
) -> None:
    people = world.people_by_email()
    sc = run_ai_suite(
        world,
        manifest["cases"],
        baseline=RulesStubV2(people),
        candidate=RulesStubV1(people),
        resamples=200,
    )
    decision, reasons = evaluate(sc)
    assert decision == "reject"
    assert any("forwarded_content_attributed_to_forwarder" in r for r in reasons)


def test_ai_runner_needs_cases(world: WorldV1) -> None:
    people = world.people_by_email()
    with pytest.raises(ValueError, match="no cases"):
        run_ai_suite(world, {}, baseline=RulesStubV1(people), candidate=RulesStubV2(people))


def test_context_replay_with_oracle_is_correct_at_every_checkpoint() -> None:
    for chain in load_chains(EVALS_DIR / CHAINS_REL):
        results = replay(chain, OracleStub())
        assert len(results) == len(chain.expected_state)
        assert all(r.state_correct for r in results), [r.field_mismatches for r in results]


def test_context_suite_end_to_end(manifest: dict[str, Any]) -> None:
    chains = load_chains(EVALS_DIR / CHAINS_REL)
    sc, results = run_context_suite(
        chains,
        manifest["chains"],
        baseline=NaiveStub(),
        candidate=OracleStub(),
        resamples=300,
    )
    quality = {q.metric: q for q in sc.quality}
    assert quality["state_accuracy"].candidate == 1.0
    assert quality["state_accuracy"].baseline < 0.5
    assert quality["duplicate_free_rate"].baseline < 1.0  # naive stub duplicates cross-source commitments
    assert sum(len(r) for r in results.values()) == sum(len(c.expected_state) for c in chains)
    assert evaluate(sc)[0] == "incomplete"
    # Swapping arms: the naive stub fails the star chain (safety, zero tolerance).
    sc_swapped, _ = run_context_suite(
        chains,
        manifest["chains"],
        baseline=OracleStub(),
        candidate=NaiveStub(),
        resamples=100,
    )
    assert evaluate(sc_swapped)[0] == "reject"


def test_cli_commands(tmp_path: Path, evals_copy: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--evals-root", str(evals_copy), "gate-a0"]) == 0
    assert main(["--evals-root", str(evals_copy), "freeze", "--version", "golden-v0.1"]) == 1
    assert "freeze refused" in capsys.readouterr().err
    assert (
        main(["--evals-root", str(evals_copy), "run-ai", "--resamples", "100", "--out", str(tmp_path / "ai")])
        == 0
    )
    assert (tmp_path / "ai" / "scorecard.json").is_file() and (tmp_path / "ai" / "scorecard.md").is_file()
    out = tmp_path / "ctx"
    assert (
        main(["--evals-root", str(evals_copy), "run-context", "--resamples", "100", "--out", str(out)]) == 0
    )
    assert json.loads((out / "checkpoints.json").read_text(encoding="utf-8"))
    eml = sorted((evals_copy / WORLD_REL / "sources" / "emails").glob("*.eml"))[0]
    eml.write_text("tampered", encoding="utf-8")
    assert main(["--evals-root", str(evals_copy), "gate-a0"]) == 1


def test_build_candidate_reproduces_the_committed_manifest(
    evals_copy: Path, manifest: dict[str, Any]
) -> None:
    shutil.rmtree(evals_copy / WORLD_REL / "sources" / "emails")
    (evals_copy / WORLD_REL / "sources" / "stale.txt").parent.mkdir(parents=True, exist_ok=True)
    (evals_copy / WORLD_REL / "sources" / "stale.txt").write_text("left over", encoding="utf-8")
    assert main(["--evals-root", str(evals_copy), "build-candidate"]) == 0
    assert load_manifest(manifest_path(evals_copy)) == manifest
    assert run_gate_a0(evals_copy, scan_dirs=[]).passed
