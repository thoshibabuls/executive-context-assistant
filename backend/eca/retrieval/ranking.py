"""Ranking inside a tier (CONTEXT_ARCHITECTURE.md §9.2, weights in §9.10). Pure functions.

``score = base * recency_decay(age, half_life[type]) * authority_weight * materiality_weight
* (1 + 0.3 * anchor_match)``. Open items do not decay; relationally matched rows have base 1.0,
discovered chunks their RRF score.
"""

from __future__ import annotations

import datetime
import math

HALF_LIFE_DAYS: dict[str, float | None] = {
    "open_item": None,
    "event": 7.0,
    "message": 14.0,
    "calendar_event": 14.0,
    "transcript": 30.0,
    "decision": 120.0,
}
AUTHORITY_WEIGHT = {"user": 1.4, "confirmed": 1.3, "explicit": 1.1, "inferred": 1.0, "summary": 0.8}
MATERIALITY_WEIGHT = {0: 0.5, 1: 0.8, 2: 1.0, 3: 1.2}
ANCHOR_BOOST = 0.3


def recency_decay(age: datetime.timedelta, kind: str) -> float:
    half_life = HALF_LIFE_DAYS.get(kind, 14.0)
    if half_life is None:
        return 1.0
    days = max(age.total_seconds(), 0.0) / 86400.0
    return math.pow(0.5, days / half_life)


def authority_class(*, origin: str, verification_status: str, user_fields: bool, strength: str | None) -> str:
    if origin == "user" or user_fields:
        return "user"
    if verification_status == "confirmed":
        return "confirmed"
    if strength == "explicit":
        return "explicit"
    return "inferred"


def score(
    *,
    base: float,
    kind: str,
    age: datetime.timedelta,
    authority: str = "inferred",
    materiality: int | None = None,
    anchor_match: bool = False,
) -> float:
    value = base * recency_decay(age, kind) * AUTHORITY_WEIGHT.get(authority, 1.0)
    if materiality is not None:
        value *= MATERIALITY_WEIGHT.get(materiality, 1.0)
    if anchor_match:
        value *= 1 + ANCHOR_BOOST
    return value
