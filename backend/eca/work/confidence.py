"""Penalty-based confidence before calibration data exists (AI_PIPELINE.md §5.3)."""

from __future__ import annotations

from dataclasses import dataclass

PENALTIES = {
    "fuzzy_grounding": 0.85,
    "owner_unresolved": 0.6,
    "date_disagreement": 0.8,
    "report_statement": 0.8,
    "forwarded": 0.8,
    "owner_inconsistent": 0.6,
}
CAP = 0.95
MIN_TO_CREATE = 0.5  # CONTEXT_ARCHITECTURE.md §12.5: detected → suggested needs confidence ≥ 0.5


@dataclass(frozen=True)
class Confidence:
    value: float
    band: str
    penalties: tuple[str, ...]


def band(value: float) -> str:
    return "low" if value < 0.6 else ("medium" if value < 0.8 else "high")


def compute(model_conf: float, penalties: list[str]) -> Confidence:
    value = model_conf
    for p in penalties:
        value *= PENALTIES[p]
    value = round(min(CAP, max(0.0, value)), 4)
    return Confidence(value, band(value), tuple(penalties))
