"""Gate A0 (AI_EVALUATION.md §9): runs on every PR.

Checks, each reported as ``pass``, ``fail`` or ``n/a``:

* ``manifest``: every covered file matches its SHA-256; no missing or unlisted file; known splits;
  no frozen version with draft labels.
* ``dataset``: ``world_v1`` labels and the chain YAML load and validate.
* ``splits``: every email and chain has a split in the manifest; the manifest agrees with the
  deterministic assignment, so related cases never cross splits.
* ``contamination``: prompts and few-shot files share no 8-gram with ``test``, ``sealed`` or
  ``challenge`` texts.
* ``cassette_suites`` and ``e14_provenance``: ``n/a`` until committed cassettes (slice 1.4) and
  AI-derived objects exist.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from eca_evals.contamination import DEFAULT_N, candidate_files, scan
from eca_evals.context.chains import Chain, ChainError, load_chains
from eca_evals.dataset import DatasetError, WorldV1, load_world_v1
from eca_evals.golden import CHAINS_REL, WORLD_REL, case_splits, chain_splits, manifest_path
from eca_evals.manifest import ManifestError, load_manifest, verify_manifest

Status = Literal["pass", "fail", "n/a"]
PROTECTED_SPLITS = ("test", "sealed", "challenge")


@dataclass
class Check:
    name: str
    status: Status
    details: list[str] = field(default_factory=list)


@dataclass
class GateResult:
    checks: list[Check]

    @property
    def passed(self) -> bool:
        return all(c.status != "fail" for c in self.checks)

    def summary(self) -> str:
        lines = [f"gate A0: {'PASS' if self.passed else 'FAIL'}"]
        for c in self.checks:
            lines.append(f"  {c.name}: {c.status}")
            lines += [f"    - {d}" for d in c.details[:20]]
        return "\n".join(lines)


def _chain_text(chain: Chain) -> str:
    return "\n".join(s.text for s in chain.sources)


def run_gate_a0(evals_root: Path, *, scan_dirs: Sequence[Path]) -> GateResult:
    checks: list[Check] = []
    try:
        manifest = load_manifest(manifest_path(evals_root))
    except ManifestError as exc:
        return GateResult([Check("manifest", "fail", [str(exc)])])
    problems = verify_manifest(evals_root, manifest)
    checks.append(
        Check(
            "manifest",
            "fail" if problems else "pass",
            [f"{p.kind}: {p.path} {p.detail}".strip() for p in problems],
        )
    )

    world: WorldV1 | None = None
    chains: list[Chain] = []
    errors: list[str] = []
    try:
        world = load_world_v1(evals_root / WORLD_REL)
    except (DatasetError, OSError) as exc:
        errors.append(f"world_v1: {exc}")
    try:
        chains = load_chains(evals_root / CHAINS_REL)
    except ChainError as exc:
        errors.append(f"chains: {exc}")
    checks.append(Check("dataset", "fail" if errors else "pass", errors))
    if world is None or errors:
        checks.append(Check("splits", "fail", ["dataset did not load"]))
        checks.append(Check("contamination", "fail", ["dataset did not load"]))
        return GateResult(checks)

    split_problems: list[str] = []
    listed_cases: dict[str, str] = manifest.get("cases", {})
    listed_chains: dict[str, str] = manifest.get("chains", {})
    for listed, expected, kind in (
        (listed_cases, case_splits(world), "case"),
        (listed_chains, chain_splits(chains), "chain"),
    ):
        for unit_id in sorted(set(expected) | set(listed)):
            if unit_id not in listed:
                split_problems.append(f"{kind} {unit_id} has no split in the manifest")
            elif unit_id not in expected:
                split_problems.append(f"{kind} {unit_id} is listed but does not exist")
            elif listed[unit_id] != expected[unit_id]:
                split_problems.append(
                    f"{kind} {unit_id}: manifest {listed[unit_id]}, assignment {expected[unit_id]}"
                )
    for thread_id, thread in sorted(world.threads.items()):
        splits = {listed_cases.get(c) for c in thread["case_ids"]}
        if len(splits) > 1:
            split_problems.append(f"thread {thread_id} crosses splits {sorted(map(str, splits))}")
    checks.append(Check("splits", "fail" if split_problems else "pass", split_problems))

    protected: dict[str, str] = {}
    dev_texts: list[str] = []
    for case in world.emails:
        text = f"{case.subject}\n{case.body}"
        if listed_cases.get(case.case_id) in PROTECTED_SPLITS:
            protected[case.case_id] = text
        elif listed_cases.get(case.case_id) == "dev":
            dev_texts.append(text)
    for chain in chains:
        if listed_chains.get(chain.id) in PROTECTED_SPLITS:
            protected[chain.id] = _chain_text(chain)
        elif listed_chains.get(chain.id) == "dev":
            dev_texts.append(_chain_text(chain))
    files = candidate_files(scan_dirs)
    hits = scan(protected, files, n=DEFAULT_N, allowed=dev_texts)
    checks.append(
        Check(
            "contamination",
            "fail" if hits else "pass",
            [f"{h.file} shares '{h.ngram}' with {h.case_id}" for h in hits]
            or [f"{len(files)} prompt/few-shot files scanned"],
        )
    )
    checks.append(Check("cassette_suites", "n/a", ["no committed cassette suites before AI-01 (slice 1.4)"]))
    checks.append(Check("e14_provenance", "n/a", ["no AI-derived objects before slice 1.4"]))
    return GateResult(checks)
