"""Priority inputs and the version-checked priority write for work items (slice 1.8).

``attention`` computes scores (TECHNICAL_DESIGN.md §12.6) and writes them through
``set_item_priority``: the row is updated only if its version is the one the inputs were read
at, so a concurrent change wins and triggers its own recompute. Priority writes do not bump the
version (they are a projection, not a change of the item).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select, update

from eca.platform.uow import UnitOfWork
from eca.work.models import work_items_table


@dataclass(frozen=True)
class ItemPriorityInput:
    id: UUID
    version: int
    direction: str
    statement_kind: str | None
    due_at: datetime.datetime | None
    confidence: float | None
    origin: str
    verification_status: str
    owner_person_id: UUID | None
    counterparty_person_id: UUID | None
    requester_person_id: UUID | None
    priority_override: int | None
    priority_score: float | None


async def priority_candidates(uow: UnitOfWork, item_ids: list[UUID] | None = None) -> list[ItemPriorityInput]:
    """Open, unrejected, unmerged items (all of the user's, or the given ones)."""
    t = work_items_table
    stmt = select(
        t.c.id,
        t.c.version,
        t.c.direction,
        t.c.statement_kind,
        t.c.due_at,
        t.c.confidence,
        t.c.origin,
        t.c.verification_status,
        t.c.owner_person_id,
        t.c.counterparty_person_id,
        t.c.requester_person_id,
        t.c.priority_override,
        t.c.priority_score,
    ).where(
        t.c.merged_into_id.is_(None),
        t.c.deleted_at.is_(None),
        ~t.c.archived,
        t.c.lifecycle_status.in_(("open", "in_progress")),
        t.c.verification_status != "rejected",
    )
    if item_ids is not None:
        stmt = stmt.where(t.c.id.in_(item_ids))
    rows = await uow.session.execute(stmt)
    return [ItemPriorityInput(**r._mapping) for r in rows]


async def set_item_priority(
    uow: UnitOfWork,
    item_id: UUID,
    *,
    score: float,
    reasons: list[dict[str, Any]],
    version: int,
    now: datetime.datetime,
) -> bool:
    t = work_items_table
    result = await uow.session.execute(
        update(t)
        .where(t.c.id == item_id, t.c.version == version)
        .values(priority_score=score, priority_reasons=reasons, priority_computed_at=now)
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def clear_closed_priority(uow: UnitOfWork) -> None:
    """Closed or rejected items drop out of priority lists."""
    t = work_items_table
    await uow.session.execute(
        update(t)
        .where(
            t.c.priority_score.is_not(None),
            (t.c.lifecycle_status.in_(("done", "cancelled"))) | (t.c.verification_status == "rejected"),
        )
        .values(priority_score=None, priority_reasons=None)
    )
