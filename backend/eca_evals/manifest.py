"""``MANIFEST.json``: file hashes, case splits, label status and freeze (AI_EVALUATION.md §3.3).

Paths are relative to ``evals/``. Verification fails on a missing file, a hash mismatch or an
unlisted file in a covered directory, so every data change goes through ``build``. ``freeze``
refuses to release a version whose labels are still draft: a human owner's review record
(``REVIEW.json``) must list reviewers and cover every label and chain file.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MANIFEST_NAME = "MANIFEST.json"
REVIEW_NAME = "REVIEW.json"
EXCLUDED_NAMES = {MANIFEST_NAME, REVIEW_NAME}


class ManifestError(Exception):
    pass


@dataclass(frozen=True)
class Problem:
    kind: str  # missing | hash_mismatch | unlisted | split | review
    path: str
    detail: str = ""


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def covered_files(evals_root: Path, covered_dirs: Iterable[str]) -> list[str]:
    files: list[str] = []
    for rel in covered_dirs:
        base = evals_root / rel
        for path in sorted(base.rglob("*")):
            if path.is_file() and path.name not in EXCLUDED_NAMES:
                files.append(path.relative_to(evals_root).as_posix())
    return sorted(files)


def build_manifest(
    evals_root: Path,
    *,
    version: str,
    covered_dirs: list[str],
    cases: dict[str, str],
    chains: dict[str, str],
    label_files: list[str],
    labels_status: str = "draft",
) -> dict[str, Any]:
    return {
        "dataset": "world_v1",
        "version": version,
        "frozen": False,
        "labels_status": labels_status,
        "label_schema_version": 1,
        "reviewers": [],
        "review_record": None,
        "notes": "Synthetic data only. Labels generated from templates; not reviewed by a human (Q12).",
        "covered_dirs": covered_dirs,
        "label_files": sorted(label_files),
        "files": {rel: sha256_file(evals_root / rel) for rel in covered_files(evals_root, covered_dirs)},
        "cases": dict(sorted(cases.items())),
        "chains": dict(sorted(chains.items())),
    }


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot read {path.name}: {exc}") from exc
    return data


def verify_manifest(evals_root: Path, manifest: dict[str, Any]) -> list[Problem]:
    problems: list[Problem] = []
    listed: dict[str, str] = manifest.get("files", {})
    for rel, expected in sorted(listed.items()):
        path = evals_root / rel
        if not path.is_file():
            problems.append(Problem("missing", rel))
        elif sha256_file(path) != expected:
            problems.append(Problem("hash_mismatch", rel))
    for rel in covered_files(evals_root, manifest.get("covered_dirs", [])):
        if rel not in listed:
            problems.append(Problem("unlisted", rel))
    allowed = {"dev", "test", "sealed", "challenge"}
    for case, split in {**manifest.get("cases", {}), **manifest.get("chains", {})}.items():
        if split not in allowed:
            problems.append(Problem("split", case, f"unknown split {split!r}"))
    if manifest.get("frozen") and manifest.get("labels_status") != "reviewed":
        problems.append(Problem("review", "MANIFEST.json", "frozen version with draft labels"))
    return problems


def freeze(evals_root: Path, manifest_path: Path, *, version: str) -> dict[str, Any]:
    """Mark the candidate as released, only with a complete human review record."""
    manifest = load_manifest(manifest_path)
    problems = verify_manifest(evals_root, manifest)
    if problems:
        raise ManifestError(f"manifest does not verify: {problems[:5]}")
    review_path = manifest_path.parent / REVIEW_NAME
    if not review_path.is_file():
        raise ManifestError(
            "labels are draft: no REVIEW.json from the labelling owner (Q12); "
            "golden versions are frozen only "
            "after human review"
        )
    review = json.loads(review_path.read_text(encoding="utf-8"))
    reviewers = [r for r in review.get("reviewers", []) if isinstance(r, str) and r.strip()]
    if not reviewers:
        raise ManifestError("REVIEW.json names no reviewer")
    reviewed = set(review.get("reviewed_files", []))
    required = set(manifest["label_files"])
    if not required <= reviewed:
        raise ManifestError(f"label files not reviewed: {sorted(required - reviewed)}")
    if not review.get("date"):
        raise ManifestError("REVIEW.json has no review date")
    manifest.update(
        {
            "version": version,
            "frozen": True,
            "labels_status": "reviewed",
            "reviewers": reviewers,
            "review_record": {
                "file": REVIEW_NAME,
                "sha256": sha256_file(review_path),
                "date": review["date"],
            },
        }
    )
    write_manifest(manifest_path, manifest)
    return manifest
