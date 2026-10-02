"""Repository locations of evaluation data (AI_EVALUATION.md §3.1, CONTEXT_EVALUATION.md §4.4)."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EVALS_DIR = REPO_ROOT / "evals"
WORLD_V1_DIR = EVALS_DIR / "ai" / "datasets" / "world_v1"
CHAINS_DIR = EVALS_DIR / "context" / "chains"
AI_REPORTS_DIR = EVALS_DIR / "ai" / "reports"
CONTEXT_REPORTS_DIR = EVALS_DIR / "context" / "reports"
PROMPTS_DIR = REPO_ROOT / "backend" / "eca" / "intelligence" / "prompts"
FEWSHOT_DIRS = (PROMPTS_DIR, EVALS_DIR / "ai" / "fewshot")
