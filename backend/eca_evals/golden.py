"""Building the ``golden-v0.1`` candidate: data, splits and manifest (AI_EVALUATION.md §3).

``build_candidate`` regenerates ``world_v1`` from its generator, assigns splits at thread and
chain level and writes ``MANIFEST.json`` with ``frozen: false`` and ``labels_status: draft``.
Releasing the version is a separate step (``manifest.freeze``) that needs the owner's review.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from eca_evals.context.chains import Chain, load_chains
from eca_evals.dataset import WorldV1, load_world_v1
from eca_evals.manifest import build_manifest, covered_files, write_manifest
from eca_evals.splits import Split, Unit, assign_splits
from eca_evals.world_v1 import build_world_v1

VERSION = "golden-v0.1"
WORLD_REL = "ai/datasets/world_v1"
CHAINS_REL = "context/chains"
COVERED_DIRS = [WORLD_REL, CHAINS_REL]


def manifest_path(evals_root: Path) -> Path:
    return evals_root / WORLD_REL / "MANIFEST.json"


def case_splits(world: WorldV1) -> dict[str, Split]:
    """Split of every email, assigned per thread (challenge-tagged threads go to ``challenge``)."""
    units = [
        Unit(t["thread_id"], str(t["stratum"]), challenge=bool(t.get("challenge_tags")))
        for t in sorted(world.threads.values(), key=lambda t: str(t["thread_id"]))
    ]
    by_thread = assign_splits(units)
    return {c.case_id: by_thread[c.thread_id] for c in world.emails}


def chain_splits(chains: list[Chain]) -> dict[str, Split]:
    return assign_splits(Unit(c.id, "chain", challenge="challenge" in c.tags) for c in chains)


def build_candidate(evals_root: Path) -> dict[str, Any]:
    """Regenerate ``world_v1`` and write the candidate manifest; returns a summary."""
    world_dir = evals_root / WORLD_REL
    for generated in (world_dir / "sources", world_dir / "labels"):
        if generated.exists():
            shutil.rmtree(generated)
    counts = build_world_v1(world_dir)
    world = load_world_v1(world_dir)
    chains = load_chains(evals_root / CHAINS_REL)
    cases, chain_assignment = case_splits(world), chain_splits(chains)
    label_files = [
        rel
        for rel in covered_files(evals_root, COVERED_DIRS)
        if rel.startswith(f"{WORLD_REL}/labels/") or rel.startswith(f"{CHAINS_REL}/")
    ]
    manifest = build_manifest(
        evals_root,
        version=VERSION,
        covered_dirs=COVERED_DIRS,
        cases=dict(cases),
        chains=dict(chain_assignment),
        label_files=label_files,
    )
    write_manifest(manifest_path(evals_root), manifest)
    return {**counts, "chains": len(chains), "files": len(manifest["files"])}
