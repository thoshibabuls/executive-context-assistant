"""Command line: ``python -m eca_evals <command>`` from ``backend/`` (AI_EVALUATION.md §3.1).

Commands:

* ``build-candidate``: regenerate ``world_v1`` and write the draft ``golden-v0.1`` manifest.
* ``gate-a0``: hashes, dataset validation, splits and contamination; exit 1 on failure.
* ``freeze --version V``: release a version; refused while labels are draft.
* ``run-ai`` / ``run-context``: run the stub pipelines end to end and write a scorecard.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from eca_evals.ai.runner import run_ai_suite
from eca_evals.ai.stubs import RulesStubV1, RulesStubV2
from eca_evals.context.chains import load_chains
from eca_evals.context.runner import NaiveStub, OracleStub, checkpoint_report, run_context_suite
from eca_evals.dataset import load_world_v1
from eca_evals.gate_a0 import run_gate_a0
from eca_evals.golden import CHAINS_REL, WORLD_REL, build_candidate, manifest_path
from eca_evals.manifest import ManifestError, freeze, load_manifest
from eca_evals.paths import AI_REPORTS_DIR, CONTEXT_REPORTS_DIR, EVALS_DIR, FEWSHOT_DIRS
from eca_evals.scorecard import evaluate, write_report
from eca_evals.stats import DEFAULT_RESAMPLES


def _report_dir(base: Path, name: str) -> Path:
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    return base / f"{stamp}_{name}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eca_evals")
    parser.add_argument("--evals-root", type=Path, default=EVALS_DIR)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("build-candidate")
    sub.add_parser("gate-a0")
    p_freeze = sub.add_parser("freeze")
    p_freeze.add_argument("--version", required=True)
    for name in ("run-ai", "run-context"):
        p = sub.add_parser(name)
        p.add_argument("--splits", default="test,challenge" if name == "run-ai" else "dev,test,sealed")
        p.add_argument("--resamples", type=int, default=DEFAULT_RESAMPLES)
        p.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    root: Path = args.evals_root

    if args.command == "build-candidate":
        print(json.dumps(build_candidate(root), sort_keys=True))
        return 0
    if args.command == "gate-a0":
        result = run_gate_a0(root, scan_dirs=FEWSHOT_DIRS)
        print(result.summary())
        return 0 if result.passed else 1
    if args.command == "freeze":
        try:
            manifest = freeze(root, manifest_path(root), version=args.version)
        except ManifestError as exc:
            print(f"freeze refused: {exc}", file=sys.stderr)
            return 1
        print(f"frozen {manifest['version']}")
        return 0

    manifest = load_manifest(manifest_path(root))
    splits = tuple(s.strip() for s in args.splits.split(",") if s.strip())
    if args.command == "run-ai":
        world = load_world_v1(root / WORLD_REL)
        people = world.people_by_email()
        sc = run_ai_suite(
            world,
            manifest["cases"],
            baseline=RulesStubV1(people),
            candidate=RulesStubV2(people),
            splits=splits,
            resamples=args.resamples,
            labels_status=manifest["labels_status"],
        )
        out = args.out or _report_dir(AI_REPORTS_DIR, "ai_email_slice")
    else:
        chains = load_chains(root / CHAINS_REL)
        sc, results = run_context_suite(
            chains,
            manifest["chains"],
            baseline=NaiveStub(),
            candidate=OracleStub(),
            splits=splits,
            resamples=args.resamples,
        )
        out = args.out or _report_dir(CONTEXT_REPORTS_DIR, "context_l2")
        out.mkdir(parents=True, exist_ok=True)
        (out / "checkpoints.json").write_text(
            json.dumps(checkpoint_report(results), indent=2) + "\n", encoding="utf-8"
        )
    json_path, _ = write_report(sc, out)
    decision, reasons = evaluate(sc)
    print(f"decision: {decision}")
    for reason in reasons:
        print(f"  - {reason}")
    print(f"report: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
