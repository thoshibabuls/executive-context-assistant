"""Priority v1: deterministic features and score (TECHNICAL_DESIGN.md §12.6). No AI call.

``score = Σ wᵢ·fᵢ`` scaled to 0-100, times ``(0.6 + 0.4·confidence)``, unless an override is set.
Unknown or bulk senders are capped without user confirmation. ``reasons`` are the top
contributing features, rendered from fixed templates (no model text).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from eca.platform.config import REPO_ROOT

DEFAULT_PRIORITY_CONFIG = REPO_ROOT / "config" / "priority.yaml"
REQUEST_TYPES = frozenset({"reply", "review", "approve", "decide", "provide_info"})

_TEMPLATES = {
    "deadline_urgency": "Due {when}",
    "sender_importance": "From someone important to you",
    "org_importance": "Organization you marked important",
    "project_importance": "Important project",
    "commitment_weight": "{direction_text}",
    "explicit_request": "Direct request to you",
    "business_impact": "{impact} business impact",
    "thread_activity": "Active conversation",
    "meeting_proximity": "You meet them within 24 hours",
}
_DIRECTION_TEXT = {
    "my_commitment": "You committed to this",
    "my_task": "Your task",
    "waiting_for": "Overdue: you are waiting on this",
    "delegated": "You delegated this",
}


@dataclass(frozen=True)
class PriorityConfig:
    version: str
    weights: dict[str, float]
    deadline: dict[str, float]
    commitment: dict[str, float]
    impact: dict[str, float]
    unknown_cap: float
    overrides: dict[int, float]
    top_n: int

    @classmethod
    def load(cls, path: Path | None = None) -> PriorityConfig:
        data: dict[str, Any] = yaml.safe_load((path or DEFAULT_PRIORITY_CONFIG).read_text())
        weights = {k: float(v) for k, v in data["weights"].items()}
        total = sum(weights.values())
        if total <= 0:
            raise ValueError("priority weights must sum to a positive number")
        return cls(
            version=str(data["version"]),
            weights={k: v / total for k, v in weights.items()},
            deadline={k: float(v) for k, v in data["deadline_urgency"].items()},
            commitment={k: float(v) for k, v in data["commitment_weight"].items()},
            impact={k: float(v) for k, v in data["business_impact"].items()},
            unknown_cap=float(data["unconfirmed_unknown_cap"]),
            overrides={int(k): float(v) for k, v in data["override_scores"].items()},
            top_n=int(data["reasons_top_n"]),
        )


@dataclass
class Features:
    values: dict[str, float] = field(default_factory=dict)
    context: dict[str, str] = field(default_factory=dict)


def deadline_urgency(cfg: PriorityConfig, due_at: datetime.datetime | None, now: datetime.datetime) -> float:
    if due_at is None:
        return 0.0
    left = due_at - now
    if left < datetime.timedelta(0):
        return cfg.deadline["overdue"]
    for key, hours in (("within_24h", 24), ("within_72h", 72), ("within_7d", 168)):
        if left <= datetime.timedelta(hours=hours):
            return cfg.deadline[key]
    return 0.0


def importance(person: dict[str, Any] | None) -> tuple[float, float, bool]:
    """(sender importance, organization importance, known sender) for one person's inputs."""
    if not person:
        return 0.0, 0.0, False
    user = person.get("importance_user")
    stats = person.get("stats") or {}
    inferred = person.get("importance_inferred")
    if inferred is None:  # profiles computed before slice 3.4 kept it in the stats document only
        inferred = stats.get("importance_inferred", 0.0)
    inferred = float(inferred or 0.0)
    sender = max((user or 0) / 5.0, min(max(inferred, 0.0), 1.0))
    org = (person.get("org_importance") or 0) / 5.0
    return sender, org, bool(person.get("known")) or bool(person.get("is_self"))


def score(
    cfg: PriorityConfig, features: Features, *, confidence: float | None, override: int | None, capped: bool
) -> tuple[float, list[dict[str, Any]]]:
    if override in cfg.overrides:
        return cfg.overrides[override], [{"feature": "priority_override", "text": "You set this priority"}]
    contributions = {k: cfg.weights.get(k, 0.0) * v for k, v in features.values.items() if v > 0}
    raw = 100.0 * sum(contributions.values())
    conf = 1.0 if confidence is None else max(0.0, min(confidence, 1.0))
    value = raw * (0.6 + 0.4 * conf)
    if capped:
        value = min(value, cfg.unknown_cap)
    top = sorted(contributions.items(), key=lambda kv: (-kv[1], kv[0]))[: cfg.top_n]
    reasons = [
        {"feature": name, "text": _TEMPLATES[name].format(**features.context), "weight": round(c * 100, 1)}
        for name, c in top
    ]
    return round(value, 2), reasons


def when_text(due_at: datetime.datetime, now: datetime.datetime) -> str:
    left = due_at - now
    if left < datetime.timedelta(0):
        return "overdue"
    if left <= datetime.timedelta(hours=24):
        return "within 24 hours"
    return f"in {max(1, left.days)} days"


def item_features(
    cfg: PriorityConfig,
    *,
    direction: str,
    statement_kind: str | None,
    due_at: datetime.datetime | None,
    people: list[dict[str, Any] | None],
    meeting_soon: bool,
    now: datetime.datetime,
) -> tuple[Features, bool]:
    """Features of a work item; second value: no known person involved (cap candidate)."""
    f = Features()
    f.values["deadline_urgency"] = deadline_urgency(cfg, due_at, now)
    if due_at is not None:
        f.context["when"] = when_text(due_at, now)
    sender, org, known = 0.0, 0.0, False
    for p in people:
        s, o, k = importance(p)
        sender, org, known = max(sender, s), max(org, o), known or k
    f.values["sender_importance"] = sender
    f.values["org_importance"] = org
    overdue = due_at is not None and due_at < now
    if direction == "waiting_for":
        weight = cfg.commitment["waiting_for_overdue"] if overdue else 0.0
    else:
        weight = cfg.commitment.get(direction, 0.0)
    f.values["commitment_weight"] = weight
    f.context["direction_text"] = _DIRECTION_TEXT.get(direction, "")
    f.values["explicit_request"] = 1.0 if statement_kind in ("request", "assignment") else 0.0
    f.values["meeting_proximity"] = 1.0 if meeting_soon else 0.0
    return f, not known


def conversation_features(
    cfg: PriorityConfig,
    *,
    request_type: str | None,
    business_impact: str | None,
    urgency_signals: tuple[str, ...],
    last_inbound_at: datetime.datetime | None,
    people: list[dict[str, Any] | None],
    meeting_soon: bool,
    now: datetime.datetime,
) -> tuple[Features, bool]:
    f = Features()
    sender, org, known, replied_before = 0.0, 0.0, False, False
    for p in people:
        s, o, k = importance(p)
        sender, org, known = max(sender, s), max(org, o), known or k
        replied_before = replied_before or bool(p and p.get("known"))
    f.values["sender_importance"] = sender
    f.values["org_importance"] = org
    f.values["explicit_request"] = 1.0 if request_type in REQUEST_TYPES else 0.0
    if business_impact in cfg.impact:
        f.values["business_impact"] = cfg.impact[business_impact]
        f.context["impact"] = business_impact.capitalize()
    if "deadline" in urgency_signals or "explicit_urgency" in urgency_signals:
        f.values["deadline_urgency"] = cfg.deadline["within_72h"]
        f.context["when"] = "soon (stated in the message)"
    active = last_inbound_at is not None and now - last_inbound_at <= datetime.timedelta(hours=72)
    f.values["thread_activity"] = 1.0 if active and replied_before else (0.5 if active else 0.0)
    f.values["meeting_proximity"] = 1.0 if meeting_soon else 0.0
    return f, not known
