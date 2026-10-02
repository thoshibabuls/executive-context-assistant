"""Source-item stage machine (BACKEND_DESIGN.md §7.5)."""

from __future__ import annotations

import datetime

STAGES = ("fetched", "normalized", "skipped", "extract_pending", "extracted", "applied", "needs_attention")
TERMINAL = frozenset({"skipped", "applied"})

ALLOWED: dict[str, frozenset[str]] = {
    "fetched": frozenset({"normalized", "skipped", "extract_pending", "needs_attention"}),
    "normalized": frozenset({"skipped", "extract_pending", "needs_attention"}),
    "extract_pending": frozenset({"extracted", "needs_attention"}),
    "extracted": frozenset({"applied", "needs_attention"}),
    "needs_attention": frozenset({"extract_pending", "extracted", "normalized"}),  # operator replay
    "skipped": frozenset(),
    "applied": frozenset({"extracted"}),  # R2 re-apply resets applied items to extracted
}

# Stage SLAs for the reconciler scan (§7.5).
SLA: dict[str, datetime.timedelta] = {
    "fetched": datetime.timedelta(minutes=5),
    "extract_pending": datetime.timedelta(minutes=15),
    "extracted": datetime.timedelta(minutes=5),
}
MAX_STAGE_ATTEMPTS = 8


class InvalidStageTransition(ValueError):
    pass


def check_transition(current: str, new: str) -> None:
    if new not in ALLOWED.get(current, frozenset()):
        raise InvalidStageTransition(f"{current} → {new} is not a valid stage transition")
