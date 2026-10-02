"""Status fold (CONTEXT_ARCHITECTURE.md §8): current item state from its ``context_events``.

Pure function, used by ``append_event`` (inside the locked transaction) and by R1 re-fold.
Rules (§8.3):
1. Events apply in ``occurred_at`` order (ties: ``recorded_at``, then ID).
2. A field set at authority 5 changes only by another authority-5 event; a disagreeing later
   event of authority ≥ 3 records a conflict instead.
3. Otherwise a later event with authority ≥ the current value's authority replaces it, except
   that an equal-authority value from a *different* person disagreeing is a conflict (two
   participants giving different dates, CC-10); the same person updating replaces (CC-02).
4. Lower-authority disagreement is a conflict only at authority ≥ 3.
5. ``lifecycle_status`` is never set by ``model``, ``system`` or ``time`` events (BACKEND_DESIGN.md
   §6.3); a model "cancelled" proposal becomes a prompt, and completion claims only set
   ``reported_status``.
6. Equal authority and equal time with different values: both kept, ``has_conflict``.
7. Fields set by the user are listed in ``user_fields``.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

FOLDED_FIELDS = (
    "type",
    "title",
    "description",
    "owner_person_id",
    "counterparty_person_id",
    "requester_person_id",
    "project_hint",
    "due_at",
    "due_precision",
    "due_text",
    "due_kind",
    "lifecycle_status",
    "verification_status",
    "commitment_strength",
    "statement_kind",
    "direction",
    "notes",
    "confidence",
    "confidence_band",
    "reported_status",
    "reported_status_at",
    "reported_status_evidence_id",
)
DUE_FIELDS = ("due_at", "due_precision", "due_text", "due_kind")
USER_ONLY = frozenset({"lifecycle_status", "verification_status", "notes"})
CONFLICT_FIELDS = frozenset({"due_at", "owner_person_id", "type", "title"})


@dataclass(frozen=True)
class FoldEvent:
    id: str
    event_type: str
    actor: str  # system | model | user | time
    authority: int
    occurred_at: datetime.datetime
    recorded_at: datetime.datetime
    payload: dict[str, Any]
    evidence_id: str | None = None


@dataclass
class _Slot:
    value: Any
    authority: int
    occurred_at: datetime.datetime
    by: str | None
    event_id: str


@dataclass
class FoldResult:
    fields: dict[str, Any] = field(default_factory=dict)
    user_fields: list[str] = field(default_factory=list)
    has_conflict: bool = False
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    due_history: list[dict[str, Any]] = field(default_factory=list)
    prompts: list[dict[str, Any]] = field(default_factory=list)
    last_activity_at: datetime.datetime | None = None
    first_evidence_at: datetime.datetime | None = None

    def state(self) -> dict[str, Any]:
        return {
            "conflicts": self.conflicts,
            "due_history": self.due_history,
            "prompts": self.prompts,
        }


def _norm(value: Any) -> Any:
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    return value


def _equal(a: Any, b: Any) -> bool:
    return bool(_norm(a) == _norm(b))


def fold(events: Iterable[FoldEvent]) -> FoldResult:
    ordered = sorted(events, key=lambda e: (e.occurred_at, e.recorded_at, e.id))
    slots: dict[str, _Slot] = {}
    result = FoldResult()
    for ev in ordered:
        result.last_activity_at = ev.occurred_at
        if ev.evidence_id and result.first_evidence_at is None:
            result.first_evidence_at = ev.occurred_at
        sets: dict[str, Any] = dict(ev.payload.get("set", {}))
        by = ev.payload.get("by_person")
        if ev.actor != "user":
            for f in USER_ONLY & set(sets):
                value = sets.pop(f)
                if f == "lifecycle_status" and value == "cancelled":
                    result.prompts.append({"kind": "possible_cancellation", "event_id": ev.id})
        # A due change moves as one group: due_at decides, the other due fields follow it.
        names = sorted(sets, key=lambda n: (n != "due_at", n))
        due_rejected = False
        for name in names:
            value = sets[name]
            if name not in FOLDED_FIELDS or (due_rejected and name in DUE_FIELDS):
                continue
            cur = slots.get(name)
            replace = False
            if cur is None:
                replace = True
            elif cur.authority == 5 and ev.authority < 5:
                if not _equal(cur.value, value) and ev.authority >= 3 and name in CONFLICT_FIELDS:
                    _conflict(result, name, cur, value, ev, kind="user_value_contradicted")
            elif ev.authority > cur.authority or ev.authority == 5:
                replace = True
            elif ev.authority == cur.authority:
                if _equal(cur.value, value):
                    replace = True
                elif ev.occurred_at == cur.occurred_at or (
                    by is not None and cur.by is not None and by != cur.by
                ):
                    if name in CONFLICT_FIELDS:
                        _conflict(result, name, cur, value, ev, kind="equal_authority")
                    else:
                        replace = True
                else:
                    replace = True
            elif not _equal(cur.value, value) and ev.authority >= 3 and name in CONFLICT_FIELDS:
                _conflict(result, name, cur, value, ev, kind="lower_authority")
            if name == "due_at" and not replace:
                due_rejected = True
            if replace:
                if name == "due_at" and cur is not None and not _equal(cur.value, value):
                    result.due_history.append(
                        {
                            "from": _norm(cur.value),
                            "to": _norm(value),
                            "event_id": ev.id,
                            "authority": ev.authority,
                        }
                    )
                slots[name] = _Slot(value, ev.authority, ev.occurred_at, by, ev.id)
    result.fields = {name: slot.value for name, slot in slots.items()}
    result.user_fields = sorted(name for name, slot in slots.items() if slot.authority == 5)
    result.has_conflict = bool(result.conflicts)
    return result


def _conflict(result: FoldResult, name: str, cur: _Slot, value: Any, ev: FoldEvent, *, kind: str) -> None:
    result.conflicts.append(
        {
            "field": name,
            "kind": kind,
            "current": _norm(cur.value),
            "current_event_id": cur.event_id,
            "other": _norm(value),
            "other_event_id": ev.id,
            "authority": ev.authority,
        }
    )
